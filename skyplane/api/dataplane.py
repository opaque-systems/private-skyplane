import json
import os
import threading
from collections import defaultdict, Counter
from datetime import datetime
from functools import partial
from datetime import datetime

import typer
import nacl.secret
import nacl.utils
import urllib3
from typing import TYPE_CHECKING, Dict, List, Optional

from skyplane import compute
from skyplane.api.tracker import TransferProgressTracker, TransferHook
from skyplane.api.transfer_job import CopyJob, SyncJob, TransferJob
from skyplane.api.config import TransferConfig
from skyplane.planner.topology import ReplicationTopology, ReplicationTopologyGateway
from skyplane.utils import logger
from skyplane.utils.definitions import gateway_docker_image, tmp_log_dir
from skyplane.utils.fn import PathLike, do_parallel

if TYPE_CHECKING:
    from skyplane.api.provisioner import Provisioner


class DataplaneAutoDeprovision:
    def __init__(self, dataplane: "Dataplane"):
        self.dataplane = dataplane

    def __enter__(self):
        return self.dataplane

    def __exit__(self, exc_type, exc_value, exc_tb):
        logger.fs.warning("Deprovisioning dataplane")
        self.dataplane.deprovision()


class Dataplane:
    """A Dataplane represents a concrete Skyplane network, including topology and VMs."""

    def __init__(
        self, clientid: str, topology: ReplicationTopology, provisioner: "Provisioner", transfer_config: TransferConfig, debug: bool = False
    ):
        """
        :param clientid: the uuid of the local host to create the dataplane
        :type clientid: str
        :param topology: the calculated topology during the transfer
        :type topology: ReplicationTopology
        :param provisioner: the provisioner to launch the VMs
        :type provisioner: Provisioner
        :param transfer_config: the configuration during the transfer
        :type transfer_config: TransferConfig
        :param debug: whether to enable debug mode, defaults to False
        :type debug: bool, optional
        """
        self.clientid = clientid
        self.topology = topology
        self.src_region_tag = self.topology.source_region()
        self.dst_region_tag = self.topology.sink_region()
        regions = Counter([node.region for node in self.topology.gateway_nodes])
        self.max_instances = int(regions[max(regions, key=regions.get)])
        self.provisioner = provisioner
        self.transfer_config = transfer_config
        self.http_pool = urllib3.PoolManager(retries=urllib3.Retry(total=3))
        self.provisioning_lock = threading.Lock()
        self.provisioned = False
        self.transfer_dir = tmp_log_dir / "transfer_logs" / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.transfer_dir.mkdir(exist_ok=True, parents=True)

        # transfer logs
        self.transfer_dir = tmp_log_dir / "transfer_logs" / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.transfer_dir.mkdir(exist_ok=True, parents=True)

        # pending tracker tasks
        self.jobs_to_dispatch: List[TransferJob] = []
        self.pending_transfers: List[TransferProgressTracker] = []
        self.bound_nodes: Dict[ReplicationTopologyGateway, compute.Server] = {}

        self.debug = debug

    def provision(
        self,
        allow_firewall: bool = True,
        gateway_docker_image: str = os.environ.get("SKYPLANE_DOCKER_IMAGE", gateway_docker_image()),
        gateway_log_dir: Optional[PathLike] = None,
        authorize_ssh_pub_key: Optional[str] = None,
        max_jobs: int = 16,
        spinner: bool = False,
    ):
        """
        Provision the transfer gateways.

        :param allow_firewall: whether to apply firewall rules in the gatweway network (default: True)
        :type allow_firewall: bool
        :param gateway_docker_image: Docker image token in github
        :type gateway_docker_image: str
        :param gateway_log_dir: path to the log directory in the remote gatweways
        :type gateway_log_dir: PathLike
        :param authorize_ssh_pub_key: authorization ssh key to the remote gateways
        :type authorize_ssh_pub_key: str
        :param max_jobs: maximum number of provision jobs to launch concurrently (default: 16)
        :type max_jobs: int
        :param spinner: whether to show the spinner during the job (default: False)
        :type spinner: bool
        """
        with self.provisioning_lock:
            if self.provisioned:
                logger.error("Cannot provision dataplane, already provisioned!")
                return
            is_aws_used = any(n.region.startswith("aws:") for n in self.topology.nodes)
            is_azure_used = any(n.region.startswith("azure:") for n in self.topology.nodes)
            is_gcp_used = any(n.region.startswith("gcp:") for n in self.topology.nodes)

            # create VMs from the topology
            for node in self.topology.gateway_nodes:
                cloud_provider, region = node.region.split(":")
                self.provisioner.add_task(
                    cloud_provider=cloud_provider,
                    region=region,
                    vm_type=getattr(self.transfer_config, f"{cloud_provider}_instance_class"),
                    spot=getattr(self.transfer_config, f"{cloud_provider}_use_spot_instances"),
                    autoterminate_minutes=self.transfer_config.autoterminate_minutes,
                )

            # initialize clouds
            self.provisioner.init_global(aws=is_aws_used, azure=is_azure_used, gcp=is_gcp_used)

            # provision VMs
            uuids = self.provisioner.provision(
                authorize_firewall=allow_firewall,
                max_jobs=max_jobs,
                spinner=spinner,
            )

            # bind VMs to nodes
            servers = [self.provisioner.get_node(u) for u in uuids]
            servers_by_region = defaultdict(list)
            for s in servers:
                servers_by_region[s.region_tag].append(s)
            for node in self.topology.gateway_nodes:
                instance = servers_by_region[node.region].pop()
                self.bound_nodes[node] = instance
            logger.fs.debug(f"[Dataplane.provision] bound_nodes = {self.bound_nodes}")
            gateway_bound_nodes = self.bound_nodes.copy()

            # start gateways
            self.provisioned = True

        def _start_gateway(
            gateway_node: ReplicationTopologyGateway,
            gateway_server: compute.Server,
        ):
            # map outgoing ports
            setup_args = {}
            for n, v in self.topology.get_outgoing_paths(gateway_node).items():
                if isinstance(n, ReplicationTopologyGateway):
                    # use private ips for gcp to gcp connection
                    src_provider, dst_provider = gateway_node.region.split(":")[0], n.region.split(":")[0]
                    if src_provider == dst_provider and src_provider == "gcp":
                        setup_args[self.bound_nodes[n].private_ip()] = v
                    else:
                        setup_args[self.bound_nodes[n].public_ip()] = v
            am_source = gateway_node in self.topology.source_instances()
            am_sink = gateway_node in self.topology.sink_instances()
            logger.fs.debug(f"[Dataplane._start_gateway] Setup args for {gateway_node}: {setup_args}")

            # start gateway
            if gateway_log_dir:
                gateway_server.init_log_files(gateway_log_dir)
            if authorize_ssh_pub_key:
                gateway_server.copy_public_key(authorize_ssh_pub_key)
            gateway_server.start_gateway(
                setup_args,
                gateway_docker_image=gateway_docker_image,
                e2ee_key_bytes=e2ee_key_bytes if (self.transfer_config.use_e2ee and (am_source or am_sink)) else None,
                use_bbr=self.transfer_config.use_bbr,
                use_compression=self.transfer_config.use_compression,
                use_socket_tls=self.transfer_config.use_socket_tls,
            )

        # todo: move server.py:start_gateway here
        logger.fs.info(f"Using docker image {gateway_docker_image}")
        e2ee_key_bytes = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)

        jobs = []
        for node, server in gateway_bound_nodes.items():
            jobs.append(partial(_start_gateway, node, server))
        logger.fs.debug(f"[Dataplane.provision] Starting gateways on {len(jobs)} servers")
        do_parallel(lambda fn: fn(), jobs, n=-1, spinner=spinner, spinner_persist=spinner, desc="Starting gateway container on VMs")

    def copy_gateway_logs(self):
        # copy logs from all gateways in parallel
        def copy_log(instance):
            typer.secho(f"Downloading log: {self.transfer_dir}/gateway_{instance.uuid()}.stdout", fg="bright_black")
            typer.secho(f"Downloading log: {self.transfer_dir}/gateway_{instance.uuid()}.stderr", fg="bright_black")

            instance.run_command("sudo docker logs -t skyplane_gateway 2> /tmp/gateway.stderr > /tmp/gateway.stdout")
            instance.download_file("/tmp/gateway.stdout", self.transfer_dir / f"gateway_{instance.uuid()}.stdout")
            instance.download_file("/tmp/gateway.stderr", self.transfer_dir / f"gateway_{instance.uuid()}.stderr")

        do_parallel(copy_log, self.bound_nodes.values(), n=-1)

    def deprovision(self, max_jobs: int = 64, spinner: bool = False):
        """
        Deprovision the remote gateways

        :param max_jobs: maximum number of jobs to deprovision the remote gateways (default: 64)
        :type max_jobs: int
        :param spinner: Whether to show the spinner during the job (default: False)
        :type spinner: bool
        """
        with self.provisioning_lock:
            if self.debug:
                logger.fs.info(f"Copying gateway logs to {self.transfer_dir}")
                self.copy_gateway_logs()

            if not self.provisioned:
                logger.fs.warning("Attempting to deprovision dataplane that is not provisioned, this may be from auto_deprovision.")
            # wait for tracker tasks
            try:
                for task in self.pending_transfers:
                    logger.fs.warning(f"Before deprovisioning, waiting for jobs to finish: {list(task.jobs.keys())}")
                    task.join()
            except KeyboardInterrupt:
                logger.warning("Interrupted while waiting for transfers to finish, deprovisioning anyway.")
                raise
            finally:
                self.provisioner.deprovision(
                    max_jobs=max_jobs,
                    spinner=spinner,
                )
                self.provisioned = False

    def check_error_logs(self) -> Dict[str, List[str]]:
        """Get the error log from remote gateways if there is any error."""

        def get_error_logs(args):
            _, instance = args
            reply = self.http_pool.request("GET", f"{instance.gateway_api_url}/api/v1/errors")
            if reply.status != 200:
                raise Exception(f"Failed to get error logs from gateway instance {instance.instance_name()}: {reply.data.decode('utf-8')}")
            return json.loads(reply.data.decode("utf-8"))["errors"]

        errors: Dict[str, List[str]] = {}
        for (_, instance), result in do_parallel(get_error_logs, self.bound_nodes.items(), n=-1):
            errors[instance] = result
        return errors

    def auto_deprovision(self) -> DataplaneAutoDeprovision:
        """Returns a context manager that will automatically call deprovision upon exit."""
        return DataplaneAutoDeprovision(self)

    def source_gateways(self) -> List[compute.Server]:
        """Returns a list of source gateway nodes"""
        return [self.bound_nodes[n] for n in self.topology.source_instances()] if self.provisioned else []

    def sink_gateways(self) -> List[compute.Server]:
        """Returns a list of sink gateway nodes"""
        return [self.bound_nodes[n] for n in self.topology.sink_instances()] if self.provisioned else []

    # def copy_log(self, instance):
    #    typer.secho(f"Downloading log: {self.transfer_dir}/gateway_{instance.uuid()}.stdout", fg="bright_black")
    #    typer.secho(f"Downloading log: {self.transfer_dir}/gateway_{instance.uuid()}.stderr", fg="bright_black")
    #    instance.run_command("sudo docker logs -t skyplane_gateway 2> /tmp/gateway.stderr > /tmp/gateway.stdout")
    #    instance.download_file("/tmp/gateway.stdout", self.transfer_dir / f"gateway_{instance.uuid()}.stdout")
    #    instance.download_file("/tmp/gateway.stderr", self.transfer_dir / f"gateway_{instance.uuid()}.stderr")

    def queue_copy(
        self,
        src: str,
        dst: str,
        recursive: bool = False,
    ) -> str:
        """
        Add a copy job to job list.
        Return the uuid of the job.

        :param src: source prefix to copy from
        :type src: str
        :param dst: the destination of the transfer
        :type dst: str
        :param recursive: if true, will copy objects at folder prefix recursively (default: False)
        :type recursive: bool
        """
        job = CopyJob(src, dst, recursive, requester_pays=self.transfer_config.requester_pays)
        logger.fs.debug(f"[SkyplaneClient] Queued copy job {job}")
        self.jobs_to_dispatch.append(job)
        return job.uuid

    def queue_sync(
        self,
        src: str,
        dst: str,
    ) -> str:
        """
        Add a sync job to job list.
        Return the uuid of the job.

        :param src: Source prefix to copy from
        :type src: str
        :param dst: The destination of the transfer
        :type dst: str
        :param recursive: If true, will copy objects at folder prefix recursively (default: False)
        :type recursive: bool
        """
        job = SyncJob(src, dst, recursive=True, requester_pays=self.transfer_config.requester_pays)
        logger.fs.debug(f"[SkyplaneClient] Queued sync job {job}")
        self.jobs_to_dispatch.append(job)
        return job.uuid

    def run_async(self, hooks: Optional[TransferHook] = None) -> TransferProgressTracker:
        """Start the transfer asynchronously. The main thread will not be blocked.

        :param hooks: Tracks the status of the transfer
        :type hooks: TransferHook
        """
        if not self.provisioned:
            logger.error("Dataplane must be pre-provisioned. Call dataplane.provision() before starting a transfer")
        tracker = TransferProgressTracker(self, self.jobs_to_dispatch, self.transfer_config, hooks)
        self.pending_transfers.append(tracker)
        tracker.start()
        logger.fs.info(f"[SkyplaneClient] Started async transfer with {len(self.jobs_to_dispatch)} jobs")
        self.jobs_to_dispatch = []
        return tracker

    def run(self, hooks: Optional[TransferHook] = None):
        """Start the transfer in the main thread. Wait until the transfer is complete.

        :param hooks: Tracks the status of the transfer
        :type hooks: TransferHook
        """
        tracker = self.run_async(hooks)
        logger.fs.debug(f"[SkyplaneClient] Waiting for transfer to complete")
        tracker.join()

    def estimate_total_cost(self):
        """Estimate total cost of queued jobs"""
        total_size = 0
        for job in self.jobs_to_dispatch:
            total_size += job.size_gb()
        return total_size * self.topology.cost_per_gb
