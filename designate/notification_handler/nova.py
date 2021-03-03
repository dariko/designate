# -*- coding: utf-8 -*-

# Copyright 2016 Telefónica Investigación y Desarrollo, S.A.U
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_config import cfg
from oslo_log import log as logging

from designate.context import DesignateContext
from designate import exceptions
from designate.notification_handler.base import NotificationHandler
from designate.objects import Record
from designate.objects import RecordSet
from designate.objects import RecordList

LOG = logging.getLogger(__name__)

class BaseEnhancedHandler(NotificationHandler):
    """Base Enhanced Handler"""

    def get_exchange_topics(self):
        exchange = cfg.CONF[self.name].control_exchange
        topics = [topic for topic in cfg.CONF[self.name].notification_topics]
        return (exchange, topics)

    def _get_context(self, tenant_id=None):
        if tenant_id:
            return DesignateContext.get_admin_context(tenant=tenant_id, edit_managed_records=True)
        else:
            return DesignateContext.get_admin_context(all_tenants=True, edit_managed_records=True)

    def _get_zone(self, context, hostname):
        tenant_zones =  self.central_api.find_zones(context, {'tenant_id': context.tenant})
        valid_zones = [x for x in tenant_zones 
                       if hostname.endswith(x.name[:-1])
                       and (not x.name in cfg.CONF[self.name].zone_id.split(','))]
        if not valid_zones:
            raise exceptions.ZoneNotFound()
        return sorted(valid_zones, key=lambda x: x.name, reverse=True)[0]

    def _get_reverse_fqdn(self, address, version):
        if version == 6:
            # See https://en.wikipedia.org/wiki/IPv6_address#IPv6_addresses_in_the_Domain_Name_System
            # Convert fdda:5cc1:23:4::1f into f100000000000000400032001cc5addf
            revaddr = map(lambda x: x.ljust(4, '0'), address[::-1].split(':')).join('')
            # Add a dot between each character and the suffix for IP v6
            return list(revaddr).join('.') + '.ip6.arpa.'
        else:
            return '.'.join(address.split('.')[::-1]) + '.in-addr.arpa.'

    def _get_reverse_zones(self, host_reverse_fqdn=None):
        context = self._get_context()
        # TODO: Test if we could get reverse_domains directly with:
        # reverse_domains = self.central_api.find_domains(context, {'name': '*.arpa.'})
        zones = self.central_api.find_zones(context)
        reverse_zones = filter(lambda x: x.name.endswith('.arpa.'), zones)
        if host_reverse_fqdn:
            return filter(lambda x: host_reverse_fqdn.endswith(x.name), reverse_zones)
        else:
            return reverse_zones

    def _get_host_fqdn(self, zone, hostname, interface):
        return "%s." % hostname

    def _create_or_update_recordset(self, context, records, zone_id, name,
                                    type, ttl=None):
        name = name.encode('idna').decode('utf-8')

        try:
            # Attempt to create a new recordset.
            values = {
                'name': name,
                'type': type,
                'ttl': ttl,
            }
            recordset = RecordSet(**values)
            recordset.records = RecordList(objects=records)
            recordset = self.central_api.create_recordset(
                context, zone_id, recordset
            )
        except exceptions.DuplicateRecordSet:
            # Fetch and update the existing recordset.
            recordset = self.central_api.find_recordset(context, {
                'zone_id': zone_id,
                'name': name,
                'type': type,
            })
            # for record in records:
                # recordset.records.append(record)
            LOG.error('aaaa %s', dir(recordset.records))
            recordset.records = records
            recordset = self.central_api.update_recordset(
                context, recordset
            )
        LOG.debug('Creating record in %s / %s', zone_id, recordset['id'])
        return recordset


    def _create_record(self, context, managed, zone, host_fqdn, interface):
        LOG.info('Create record for host: %s and interface: %s', host_fqdn, interface['label'])
        recordset_type = 'AAAA' if interface['version'] == 6 else 'A'
        # try:
        record = Record(**dict(managed, data=interface['address']))
        self._create_or_update_recordset(context, [record], zone.id,
                                         host_fqdn, recordset_type)
            # recordset = self.central_api.create_recordset(context,
                                                          # zone['id'],
                                                          # RecordSet(name=host_fqdn, type=recordset_type))
        # except exceptions.DuplicateRecordSet:
            # LOG.warn('The record: %s was already registered', host_fqdn)
        # else:
            # record_values = dict(managed, data=interface['address'])
            # LOG.info('Creating record in %s / %s with values %r', zone['id'], recordset['id'], record_values)
            # self.central_api.create_record(context,
                                           # zone['id'],
                                           # recordset['id'],
                                           # Record(**record_values))

    def _create_reverse_record(self, context, managed, host_fqdn, interface):
        LOG.info('Create reverse record for interface: %s and address: %s', interface['label'], interface['address'])
        host_reverse_fqdn = self._get_reverse_fqdn(interface['address'], interface['version'])
        LOG.info('Create reverse record: %s', host_reverse_fqdn)
        reverse_zones = self._get_reverse_zones(host_reverse_fqdn)
        admin_context = DesignateContext.get_admin_context(all_tenants=True)
        for reverse_zone in reverse_zones:
            LOG.info('Create reverse record for zone: %s', reverse_zone.name)
            try:
                recordset = self.central_api.create_recordset(admin_context,
                                                              reverse_zone.id,
                                                              RecordSet(name=host_reverse_fqdn, type='PTR'))
            except exceptions.DuplicateRecordSet:
                LOG.warn('The reverse record: %s was already registered, '
                         'deleting it', host_reverse_fqdn)
                old_reverse = self.central_api.find_recordset(
                    admin_context, {'type': 'PTR', 'name': host_reverse_fqdn})
                self.central_api.delete_recordset(
                    admin_context, reverse_zone.id, old_reverse.id)
            record_values = dict(managed, data=host_fqdn)
            LOG.debug('Creating reverse record in %s / %s with values %r',
                      reverse_zone.id, recordset['id'], record_values)
            self.central_api.create_record(admin_context,
                                           reverse_zone.id,
                                           recordset['id'],
                                           Record(**record_values))

    def _create_records(self, context, managed, payload):
        try:
            hostname = payload['hostname']
            zone = self._get_zone(context, hostname)
        except exceptions.ZoneNotFound:
            LOG.warn('There is no zone registered for tenant: %s', context.tenant)
        except Exception as e:
            LOG.error('Error getting the zone for tenant: %s. %s', context.tenant, e)
        else:
            LOG.info('Creating records for host: %s in tenant: %s using zone: %s',
                     hostname, context.tenant, zone['name'])
            ipv4_interfaces = [x for x in payload['fixed_ips'] if x['version'] == 4]
            ipv6_interfaces = [x for x in payload['fixed_ips'] if x['version'] == 6]
            if ipv4_interfaces:
                self._create_record(context, managed, zone, "%s." % hostname,
                                    ipv4_interfaces[0])
                self._create_reverse_record(context, managed, "%s." % hostname,
                                    ipv4_interfaces[0])
            if ipv6_interfaces:
                self._create_record(context, managed, zone, "%s." % hostname,
                                    ipv6_interfaces[0])
                self._create_reverse_record(context, managed, "%s." % hostname,
                                    ipv6_interfaces[0])

    def _delete_records(self, all_tenants_context, managed, payload):
        managed_records = self.central_api.find_records(all_tenants_context, managed)
        ptr_records = self.central_api.find_records(all_tenants_context, 
            {'data': '%s.' % payload['hostname']})
        LOG.error('ptr_records: %s' % [x.data for x in ptr_records])
        records = managed_records
        if len(records) == 0:
            LOG.info('No record found to be deleted')
        else:
            for record in records:
                LOG.info('Deleting record %s', record['id'])
                try:
                    self.central_api.delete_record(all_tenants_context,
                                                   record['zone_id'],
                                                   record['recordset_id'],
                                                   record['id'])
                except exceptions.ZoneNotFound:
                    LOG.warn('There is no zone registered with id: %s', record['zone_id'])
                except Exception as e:
                    LOG.error('Error deleting record: %s. %s', record['id'], e)


class NovaFixedHandler(BaseEnhancedHandler):
    """Nova Enhanced Handler"""
    __plugin_name__ = 'nova_fixed'

    def get_event_types(self):
        return [
            'compute.instance.create.end',
            'compute.instance.delete.start',
        ]

    def process_notification(self, ctx, event_type, payload):
        LOG.info('NovaCustomHandler notification: %s. %s', event_type, payload)
        tenant_id = payload['tenant_id']

        managed = {
            'managed': True,
            'managed_plugin_name': self.get_plugin_name(),
            'managed_plugin_type': self.get_plugin_type(),
            'managed_resource_type': 'instance',
            'managed_resource_id': payload['instance_id']
        }
        if event_type == 'compute.instance.create.end':
            self._create_records(self._get_context(tenant_id), managed, payload)
        elif event_type == 'compute.instance.delete.start':
            self._delete_records(self._get_context(), managed, payload)
