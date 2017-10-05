# Copyright 2014: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from boto import exception as boto_exception
from neutronclient.common import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exc
from oslo_config import cfg
from saharaclient.api import base as saharaclient_base

from rally.common import logging
from rally import consts
from rally.plugins.openstack.cleanup import base
from rally.plugins.openstack.services.identity import identity
from rally.plugins.openstack.services.image import glance_v2
from rally.plugins.openstack.services.image import image
from rally.task import utils as task_utils

CONF = cfg.CONF
CONF.import_opt("glance_image_delete_timeout",
                "rally.plugins.openstack.scenarios.glance.utils",
                "benchmark")
CONF.import_opt("glance_image_delete_poll_interval",
                "rally.plugins.openstack.scenarios.glance.utils",
                "benchmark")

LOG = logging.getLogger(__name__)


def get_order(start):
    return iter(range(start, start + 99))


class SynchronizedDeletion(object):

    def is_deleted(self):
        return True


class QuotaMixin(SynchronizedDeletion, base.ResourceManager):
    # NOTE(andreykurilin): Quotas resources are quite complex in terms of
    #   cleanup. First of all, they do not have name, id fields at all. There
    #   is only one identifier - reference to Keystone Project/Tenant. Also,
    #   we should remove them in case of existing users case... To cover both
    #   cases we should use project name as name field (it will allow to pass
    #   existing users case) and project id as id of resource

    def list(self):
        if not self.tenant_uuid:
            return []
        client = self._admin_required and self.admin or self.user
        project = identity.Identity(client).get_project(self.tenant_uuid)
        return [project]


# MAGNUM

_magnum_order = get_order(80)


@base.resource(service=None, resource=None)
class MagnumMixin(base.ResourceManager):

    def id(self):
        """Returns id of resource."""
        return self.raw_resource.uuid

    def list(self):
        result = []
        marker = None
        while True:
            resources = self._manager().list(marker=marker)
            if not resources:
                break
            result.extend(resources)
            marker = resources[-1].uuid
        return result


@base.resource("magnum", "clusters", order=next(_magnum_order),
               tenant_resource=True)
class MagnumCluster(MagnumMixin):
    """Resource class for Magnum cluster."""


@base.resource("magnum", "cluster_templates", order=next(_magnum_order),
               tenant_resource=True)
class MagnumClusterTemplate(MagnumMixin):
    """Resource class for Magnum cluster_template."""


# HEAT

@base.resource("heat", "stacks", order=100, tenant_resource=True)
class HeatStack(base.ResourceManager):
    def name(self):
        return self.raw_resource.stack_name


# SENLIN

_senlin_order = get_order(150)


@base.resource(service=None, resource=None, admin_required=True)
class SenlinMixin(base.ResourceManager):

    def id(self):
        return self.raw_resource["id"]

    def _manager(self):
        client = self._admin_required and self.admin or self.user
        return getattr(client, self._service)()

    def list(self):
        return getattr(self._manager(), self._resource)()

    def delete(self):
        # make singular form of resource name from plural form
        res_name = self._resource[:-1]
        return getattr(self._manager(), "delete_%s" % res_name)(self.id())


@base.resource("senlin", "clusters",
               admin_required=True, order=next(_senlin_order))
class SenlinCluster(SenlinMixin):
    """Resource class for Senlin Cluster."""


@base.resource("senlin", "profiles", order=next(_senlin_order),
               admin_required=False, tenant_resource=True)
class SenlinProfile(SenlinMixin):
    """Resource class for Senlin Profile."""


# NOVA

_nova_order = get_order(200)


@base.resource("nova", "servers", order=next(_nova_order),
               tenant_resource=True)
class NovaServer(base.ResourceManager):
    def list(self):
        """List all servers."""
        # FIX(boris-42): Use limit=-1 when it's fixed
        return self._manager().list()

    def delete(self):
        if getattr(self.raw_resource, "OS-EXT-STS:locked", False):
            self.raw_resource.unlock()
        super(NovaServer, self).delete()


@base.resource("nova", "server_groups", order=next(_nova_order),
               tenant_resource=True)
class NovaServerGroups(base.ResourceManager):
    pass


@base.resource("nova", "keypairs", order=next(_nova_order))
class NovaKeypair(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("nova", "quotas", order=next(_nova_order),
               admin_required=True, tenant_resource=True)
class NovaQuotas(QuotaMixin):
    pass


@base.resource("nova", "flavors", order=next(_nova_order),
               admin_required=True, perform_for_admin_only=True)
class NovaFlavors(base.ResourceManager):
    pass

    def is_deleted(self):
        try:
            self._manager().get(self.name())
        except nova_exc.NotFound:
            return True

        return False


@base.resource("nova", "aggregates", order=next(_nova_order),
               admin_required=True, perform_for_admin_only=True)
class NovaAggregate(SynchronizedDeletion, base.ResourceManager):

    def delete(self):
        for host in self.raw_resource.hosts:
            self.raw_resource.remove_host(host)
        super(NovaAggregate, self).delete()


# EC2

_ec2_order = get_order(250)


class EC2Mixin(object):

    def _manager(self):
        return getattr(self.user, self._service)()


@base.resource("ec2", "servers", order=next(_ec2_order))
class EC2Server(EC2Mixin, base.ResourceManager):

    def is_deleted(self):
        try:
            instances = self._manager().get_only_instances(
                instance_ids=[self.id()])
        except boto_exception.EC2ResponseError as e:
            # NOTE(wtakase): Nova EC2 API returns 'InvalidInstanceID.NotFound'
            #                if instance not found. In this case, we consider
            #                instance has already been deleted.
            return getattr(e, "error_code") == "InvalidInstanceID.NotFound"

        # NOTE(wtakase): After instance deletion, instance can be 'terminated'
        #                state. If all instance states are 'terminated', this
        #                returns True. And if get_only_instances() returns an
        #                empty list, this also returns True because we consider
        #                instance has already been deleted.
        return all(map(lambda i: i.state == "terminated", instances))

    def delete(self):
        self._manager().terminate_instances(instance_ids=[self.id()])

    def list(self):
        return self._manager().get_only_instances()


# NEUTRON

_neutron_order = get_order(300)


@base.resource(service=None, resource=None, admin_required=True)
class NeutronMixin(SynchronizedDeletion, base.ResourceManager):
    # Neutron has the best client ever, so we need to override everything

    def supports_extension(self, extension):
        exts = self._manager().list_extensions().get("extensions", [])
        if any(ext.get("alias") == extension for ext in exts):
            return True
        return False

    def _manager(self):
        client = self._admin_required and self.admin or self.user
        return getattr(client, self._service)()

    def id(self):
        return self.raw_resource["id"]

    def name(self):
        return self.raw_resource["name"]

    def delete(self):
        delete_method = getattr(self._manager(), "delete_%s" % self._resource)
        delete_method(self.id())

    def list(self):
        if self._resource.endswith("y"):
            resources = self._resource[:-1] + "ies"
        else:
            resources = self._resource + "s"
        list_method = getattr(self._manager(), "list_%s" % resources)
        result = list_method(tenant_id=self.tenant_uuid)[resources]
        if self.tenant_uuid:
            result = [r for r in result if r["tenant_id"] == self.tenant_uuid]

        return result


class NeutronLbaasV1Mixin(NeutronMixin):

    def list(self):
        if self.supports_extension("lbaas"):
            return super(NeutronLbaasV1Mixin, self).list()
        return []


@base.resource("neutron", "vip", order=next(_neutron_order),
               tenant_resource=True)
class NeutronV1Vip(NeutronLbaasV1Mixin):
    pass


@base.resource("neutron", "health_monitor", order=next(_neutron_order),
               tenant_resource=True)
class NeutronV1Healthmonitor(NeutronLbaasV1Mixin):
    pass


@base.resource("neutron", "pool", order=next(_neutron_order),
               tenant_resource=True)
class NeutronV1Pool(NeutronLbaasV1Mixin):
    pass


class NeutronLbaasV2Mixin(NeutronMixin):

    def list(self):
        if self.supports_extension("lbaasv2"):
            return super(NeutronLbaasV2Mixin, self).list()
        return []


@base.resource("neutron", "loadbalancer", order=next(_neutron_order),
               tenant_resource=True)
class NeutronV2Loadbalancer(NeutronLbaasV2Mixin):

    def is_deleted(self):
        try:
            self._manager().show_loadbalancer(self.id())
        except Exception as e:
            return getattr(e, "status_code", 400) == 404

        return False


@base.resource("neutron", "bgpvpn", order=next(_neutron_order),
               admin_required=True, perform_for_admin_only=True)
class NeutronBgpvpn(NeutronMixin):
    def list(self):
        if self.supports_extension("bgpvpn"):
            return self._manager().list_bgpvpns()["bgpvpns"]
        return []


# NOTE(andreykurilin): There are scenarios which uses unified way for creating
#   and associating floating ips. They do not care about nova-net and neutron.
#   We should clean floating IPs for them, but hardcoding "neutron.floatingip"
#   cleanup resource should not work in case of Nova-Net.
#   Since we are planning to abandon support of Nova-Network in next rally
#   release, let's apply dirty workaround to handle all resources.
@base.resource("neutron", "floatingip", order=next(_neutron_order),
               tenant_resource=True)
class NeutronFloatingIP(NeutronMixin):
    def name(self):
        return base.NoName(self._resource)

    def list(self):
        if consts.ServiceType.NETWORK not in self.user.services():
            return []
        return super(NeutronFloatingIP, self).list()


@base.resource("neutron", "port", order=next(_neutron_order),
               tenant_resource=True)
class NeutronPort(NeutronMixin):
    # NOTE(andreykurilin): port is the kind of resource that can be created
    #   automatically. In this case it doesn't have name field which matches
    #   our resource name templates. But we still need to identify such
    #   resources, so let's do it by using parent resources.

    ROUTER_INTERFACE_OWNERS = ("network:router_interface",
                               "network:router_interface_distributed",
                               "network:ha_router_replicated_interface")

    ROUTER_GATEWAY_OWNER = "network:router_gateway"

    def __init__(self, *args, **kwargs):
        super(NeutronPort, self).__init__(*args, **kwargs)
        self._cache = {}

    def _get_resources(self, resource):
        if resource not in self._cache:
            resources = getattr(self._manager(), "list_%s" % resource)()
            self._cache[resource] = [r for r in resources[resource]
                                     if r["tenant_id"] == self.tenant_uuid]
        return self._cache[resource]

    def list(self):
        ports = self._get_resources("ports")
        for port in ports:
            if not port.get("name"):
                parent_name = None
                if (port["device_owner"] in self.ROUTER_INTERFACE_OWNERS or
                        port["device_owner"] == self.ROUTER_GATEWAY_OWNER):
                    # first case is a port created while adding an interface to
                    #   the subnet
                    # second case is a port created while adding gateway for
                    #   the network
                    port_router = [r for r in self._get_resources("routers")
                                   if r["id"] == port["device_id"]]
                    if port_router:
                        parent_name = port_router[0]["name"]
                # NOTE(andreykurilin): in case of existing network usage,
                #   there is no way to identify ports that was created
                #   automatically.
                # FIXME(andreykurilin): find the way to filter ports created
                #   by rally
                # elif port["device_owner"] == "network:dhcp":
                #     # port created while attaching a floating-ip to the VM
                #     if port.get("fixed_ips"):
                #         port_subnets = []
                #         for fixedip in port["fixed_ips"]:
                #             port_subnets.extend(
                #                 [sn for sn in self._get_resources("subnets")
                #                  if sn["id"] == fixedip["subnet_id"]])
                #         if port_subnets:
                #             parent_name = port_subnets[0]["name"]

                # NOTE(andreykurilin): the same case as for floating ips
                # if not parent_name:
                #    port_net = [net for net in self._get_resources("networks")
                #                if net["id"] == port["network_id"]]
                #     if port_net:
                #         parent_name = port_net[0]["name"]

                if parent_name:
                    port["parent_name"] = parent_name
        return ports

    def name(self):
        name = self.raw_resource.get("parent_name",
                                     self.raw_resource.get("name", ""))
        return name or base.NoName(self._resource)

    def delete(self):
        device_owner = self.raw_resource["device_owner"]
        if (device_owner in self.ROUTER_INTERFACE_OWNERS or
                device_owner == self.ROUTER_GATEWAY_OWNER):
            if device_owner == self.ROUTER_GATEWAY_OWNER:
                self._manager().remove_gateway_router(
                    self.raw_resource["device_id"])

            self._manager().remove_interface_router(
                self.raw_resource["device_id"], {"port_id": self.id()})
        else:
            try:
                self._manager().delete_port(self.id())
            except neutron_exceptions.PortNotFoundClient:
                # Port can be already auto-deleted, skip silently
                LOG.debug("Port %s was not deleted. Skip silently because "
                          "port can be already auto-deleted.",
                          self.id())


@base.resource("neutron", "subnet", order=next(_neutron_order),
               tenant_resource=True)
class NeutronSubnet(NeutronMixin):
    pass


@base.resource("neutron", "network", order=next(_neutron_order),
               tenant_resource=True)
class NeutronNetwork(NeutronMixin):
    pass


@base.resource("neutron", "router", order=next(_neutron_order),
               tenant_resource=True)
class NeutronRouter(NeutronMixin):
    pass


@base.resource("neutron", "security_group", order=next(_neutron_order),
               tenant_resource=True)
class NeutronSecurityGroup(NeutronMixin):
    def list(self):
        tenant_sgs = super(NeutronSecurityGroup, self).list()
        # NOTE(pirsriva): Filter out "default" security group deletion
        # by non-admin role user
        return filter(lambda r: r["name"] != "default",
                      tenant_sgs)


@base.resource("neutron", "quota", order=next(_neutron_order),
               admin_required=True, tenant_resource=True)
class NeutronQuota(QuotaMixin):

    def delete(self):
        self.admin.neutron().delete_quota(self.tenant_uuid)


# CINDER

_cinder_order = get_order(400)


@base.resource("cinder", "backups", order=next(_cinder_order),
               tenant_resource=True)
class CinderVolumeBackup(base.ResourceManager):
    pass


@base.resource("cinder", "volume_types", order=next(_cinder_order),
               admin_required=True, perform_for_admin_only=True)
class CinderVolumeType(base.ResourceManager):
    pass


@base.resource("cinder", "volume_snapshots", order=next(_cinder_order),
               tenant_resource=True)
class CinderVolumeSnapshot(base.ResourceManager):
    pass


@base.resource("cinder", "transfers", order=next(_cinder_order),
               tenant_resource=True)
class CinderVolumeTransfer(base.ResourceManager):
    pass


@base.resource("cinder", "volumes", order=next(_cinder_order),
               tenant_resource=True)
class CinderVolume(base.ResourceManager):
    pass


@base.resource("cinder", "image_volumes_cache", order=next(_cinder_order),
               admin_required=True, perform_for_admin_only=True)
class CinderImageVolumeCache(base.ResourceManager):

    def _glance(self):
        return image.Image(self.admin)

    def _manager(self):
        return self.admin.cinder().volumes

    def list(self):
        images = dict(("image-%s" % i.id, i)
                      for i in self._glance().list_images())
        return [{"volume": v, "image": images[v.name]}
                for v in self._manager().list(search_opts={"all_tenants": 1})
                if v.name in images]

    def name(self):
        return self.raw_resource["image"].name

    def id(self):
        return self.raw_resource["volume"].id


@base.resource("cinder", "quotas", order=next(_cinder_order),
               admin_required=True, tenant_resource=True)
class CinderQuotas(QuotaMixin, base.ResourceManager):
    pass


@base.resource("cinder", "qos_specs", order=next(_cinder_order),
               admin_required=True, perform_for_admin_only=True)
class CinderQos(base.ResourceManager):
    pass

# MANILA

_manila_order = get_order(450)


@base.resource("manila", "shares", order=next(_manila_order),
               tenant_resource=True)
class ManilaShare(base.ResourceManager):
    pass


@base.resource("manila", "share_networks", order=next(_manila_order),
               tenant_resource=True)
class ManilaShareNetwork(base.ResourceManager):
    pass


@base.resource("manila", "security_services", order=next(_manila_order),
               tenant_resource=True)
class ManilaSecurityService(base.ResourceManager):
    pass


# GLANCE

@base.resource("glance", "images", order=500, tenant_resource=True)
class GlanceImage(base.ResourceManager):

    def _client(self):
        return image.Image(self.admin or self.user)

    def list(self):
        images = (self._client().list_images(owner=self.tenant_uuid) +
                  self._client().list_images(status="deactivated",
                                             owner=self.tenant_uuid))
        return images

    def delete(self):
        client = self._client()
        if self.raw_resource.status == "deactivated":
            glancev2 = glance_v2.GlanceV2Service(self.admin or self.user)
            glancev2.reactivate_image(self.raw_resource.id)
        client.delete_image(self.raw_resource.id)
        task_utils.wait_for_status(
            self.raw_resource, ["deleted"],
            check_deletion=True,
            update_resource=self._client().get_image,
            timeout=CONF.benchmark.glance_image_delete_timeout,
            check_interval=CONF.benchmark.glance_image_delete_poll_interval)


# SAHARA

_sahara_order = get_order(600)


@base.resource("sahara", "job_executions", order=next(_sahara_order),
               tenant_resource=True)
class SaharaJobExecution(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "jobs", order=next(_sahara_order),
               tenant_resource=True)
class SaharaJob(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "job_binary_internals", order=next(_sahara_order),
               tenant_resource=True)
class SaharaJobBinaryInternals(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "job_binaries", order=next(_sahara_order),
               tenant_resource=True)
class SaharaJobBinary(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "data_sources", order=next(_sahara_order),
               tenant_resource=True)
class SaharaDataSource(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "clusters", order=next(_sahara_order),
               tenant_resource=True)
class SaharaCluster(base.ResourceManager):

    # Need special treatment for Sahara Cluster because of the way the
    # exceptions are described in:
    # https://github.com/openstack/python-saharaclient/blob/master/
    # saharaclient/api/base.py#L145

    def is_deleted(self):
        try:
            self._manager().get(self.id())
            return False
        except saharaclient_base.APIException as e:
            return e.error_code == 404


@base.resource("sahara", "cluster_templates", order=next(_sahara_order),
               tenant_resource=True)
class SaharaClusterTemplate(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("sahara", "node_group_templates", order=next(_sahara_order),
               tenant_resource=True)
class SaharaNodeGroup(SynchronizedDeletion, base.ResourceManager):
    pass


# CEILOMETER

@base.resource("ceilometer", "alarms", order=700, tenant_resource=True)
class CeilometerAlarms(SynchronizedDeletion, base.ResourceManager):

    def id(self):
        return self.raw_resource.alarm_id

    def list(self):
        query = [{
            "field": "project_id",
            "op": "eq",
            "value": self.tenant_uuid
        }]
        return self._manager().list(q=query)


# ZAQAR

@base.resource("zaqar", "queues", order=800)
class ZaqarQueues(SynchronizedDeletion, base.ResourceManager):

    def list(self):
        return self.user.zaqar().queues()


# DESIGNATE
_designate_order = get_order(900)


class DesignateResource(SynchronizedDeletion, base.ResourceManager):

    # TODO(boris-42): This should be handled somewhere else.
    NAME_PREFIX = "s_rally_"

    def _manager(self, resource=None):
        # Map resource names to api / client version
        resource = resource or self._resource
        version = {
            "domains": "1",
            "servers": "1",
            "records": "1",
            "recordsets": "2",
            "zones": "2"
        }[resource]

        client = self._admin_required and self.admin or self.user
        return getattr(getattr(client, self._service)(version), resource)

    def id(self):
        """Returns id of resource."""
        return self.raw_resource["id"]

    def name(self):
        """Returns name of resource."""
        return self.raw_resource["name"]

    def list(self):
        return [item for item in self._manager().list()
                if item["name"].startswith(self.NAME_PREFIX)]


@base.resource("designate", "domains", order=next(_designate_order),
               tenant_resource=True, threads=1)
class DesignateDomain(DesignateResource):
    pass


@base.resource("designate", "servers", order=next(_designate_order),
               admin_required=True, perform_for_admin_only=True, threads=1)
class DesignateServer(DesignateResource):
    pass


@base.resource("designate", "zones", order=next(_designate_order),
               tenant_resource=True, threads=1)
class DesignateZones(DesignateResource):

    def list(self):
        marker = None
        criterion = {"name": "%s*" % self.NAME_PREFIX}

        while True:
            items = self._manager().list(marker=marker, limit=100,
                                         criterion=criterion)
            if not items:
                break
            for item in items:
                yield item
            marker = items[-1]["id"]


# SWIFT

_swift_order = get_order(1000)


class SwiftMixin(SynchronizedDeletion, base.ResourceManager):

    def _manager(self):
        client = self._admin_required and self.admin or self.user
        return getattr(client, self._service)()

    def id(self):
        return self.raw_resource

    def name(self):
        # NOTE(stpierre): raw_resource is a list of either [container
        # name, object name] (as in SwiftObject) or just [container
        # name] (as in SwiftContainer).
        return self.raw_resource[-1]

    def delete(self):
        delete_method = getattr(self._manager(), "delete_%s" % self._resource)
        # NOTE(weiwu): *self.raw_resource is required because for deleting
        # container we are passing only container name, to delete object we
        # should pass as first argument container and second is object name.
        delete_method(*self.raw_resource)


@base.resource("swift", "object", order=next(_swift_order),
               tenant_resource=True)
class SwiftObject(SwiftMixin):

    def list(self):
        object_list = []
        containers = self._manager().get_account(full_listing=True)[1]
        for con in containers:
            objects = self._manager().get_container(con["name"],
                                                    full_listing=True)[1]
            for obj in objects:
                raw_resource = [con["name"], obj["name"]]
                object_list.append(raw_resource)
        return object_list


@base.resource("swift", "container", order=next(_swift_order),
               tenant_resource=True)
class SwiftContainer(SwiftMixin):

    def list(self):
        containers = self._manager().get_account(full_listing=True)[1]
        return [[con["name"]] for con in containers]


# MISTRAL

_mistral_order = get_order(1100)


class MistralMixin(SynchronizedDeletion, base.ResourceManager):

    def delete(self):
        self._manager().delete(self.raw_resource["id"])


@base.resource("mistral", "workbooks", order=next(_mistral_order),
               tenant_resource=True)
class MistralWorkbooks(MistralMixin):
    def delete(self):
        self._manager().delete(self.raw_resource["name"])


@base.resource("mistral", "workflows", order=next(_mistral_order),
               tenant_resource=True)
class MistralWorkflows(MistralMixin):
    pass


@base.resource("mistral", "executions", order=next(_mistral_order),
               tenant_resource=True)
class MistralExecutions(MistralMixin):
    pass


# MURANO

_murano_order = get_order(1200)


@base.resource("murano", "environments", tenant_resource=True,
               order=next(_murano_order))
class MuranoEnvironments(SynchronizedDeletion, base.ResourceManager):
    pass


@base.resource("murano", "packages", tenant_resource=True,
               order=next(_murano_order))
class MuranoPackages(base.ResourceManager):
    def list(self):
        return filter(lambda x: x.name != "Core library",
                      super(MuranoPackages, self).list())


# IRONIC

_ironic_order = get_order(1300)


@base.resource("ironic", "node", admin_required=True,
               order=next(_ironic_order), perform_for_admin_only=True)
class IronicNodes(base.ResourceManager):

    def id(self):
        return self.raw_resource.uuid


# WATCHER

_watcher_order = get_order(1500)


class WatcherMixin(SynchronizedDeletion, base.ResourceManager):

    def id(self):
        return self.raw_resource.uuid

    def list(self):
        return self._manager().list(limit=0)

    def is_deleted(self):
        from watcherclient.common.apiclient import exceptions
        try:
            self._manager().get(self.id())
            return False
        except exceptions.NotFound:
            return True


@base.resource("watcher", "audit_template", order=next(_watcher_order),
               admin_required=True, perform_for_admin_only=True)
class WatcherTemplate(WatcherMixin):
    pass


@base.resource("watcher", "action_plan", order=next(_watcher_order),
               admin_required=True, perform_for_admin_only=True)
class WatcherActionPlan(WatcherMixin):

    def name(self):
        return base.NoName(self._resource)


@base.resource("watcher", "audit", order=next(_watcher_order),
               admin_required=True, perform_for_admin_only=True)
class WatcherAudit(WatcherMixin):

    def name(self):
        return self.raw_resource.uuid


# KEYSTONE

_keystone_order = get_order(9000)


class KeystoneMixin(SynchronizedDeletion):

    def _manager(self):
        return identity.Identity(self.admin)

    def delete(self):
        delete_method = getattr(self._manager(), "delete_%s" % self._resource)
        delete_method(self.id())

    def list(self):
        resources = self._resource + "s"
        return getattr(self._manager(), "list_%s" % resources)()


@base.resource("keystone", "user", order=next(_keystone_order),
               admin_required=True, perform_for_admin_only=True)
class KeystoneUser(KeystoneMixin, base.ResourceManager):
    pass


@base.resource("keystone", "project", order=next(_keystone_order),
               admin_required=True, perform_for_admin_only=True)
class KeystoneProject(KeystoneMixin, base.ResourceManager):
    pass


@base.resource("keystone", "service", order=next(_keystone_order),
               admin_required=True, perform_for_admin_only=True)
class KeystoneService(KeystoneMixin, base.ResourceManager):
    pass


@base.resource("keystone", "role", order=next(_keystone_order),
               admin_required=True, perform_for_admin_only=True)
class KeystoneRole(KeystoneMixin, base.ResourceManager):
    pass


# NOTE(andreykurilin): unfortunately, ec2 credentials doesn't have name
#   and id fields. It makes impossible to identify resources belonging to
#   particular task.
@base.resource("keystone", "ec2", tenant_resource=True,
               order=next(_keystone_order))
class KeystoneEc2(SynchronizedDeletion, base.ResourceManager):
    def _manager(self):
        return identity.Identity(self.user)

    def id(self):
        return "n/a"

    def name(self):
        return base.NoName(self._resource)

    @property
    def user_id(self):
        return self.user.keystone.auth_ref.user_id

    def list(self):
        return self._manager().list_ec2credentials(self.user_id)

    def delete(self):
        self._manager().delete_ec2credential(
            self.user_id, access=self.raw_resource.access)
