from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, cast

import kubernetes

import dagster._check as check
from dagster._config import process_config
from dagster.core.errors import DagsterInvalidConfigError
from dagster.core.storage.pipeline_run import PipelineRun
from dagster.core.utils import parse_env_var
from dagster.utils import make_readonly_value, merge_dicts

if TYPE_CHECKING:
    from . import K8sRunLauncher

from .job import DagsterK8sJobConfig
from .models import k8s_snake_case_dict


def _dedupe_list(values):
    return list(set([make_readonly_value(value) for value in values]))


class K8sContainerContext(
    NamedTuple(
        "_K8sContainerContext",
        [
            ("image_pull_policy", Optional[str]),
            ("image_pull_secrets", List[Dict[str, str]]),
            ("service_account_name", Optional[str]),
            ("env_config_maps", List[str]),
            ("env_secrets", List[str]),
            ("env_vars", List[str]),
            ("volume_mounts", List[Dict[str, Any]]),
            ("volumes", List[Dict[str, Any]]),
            ("labels", Dict[str, str]),
            ("namespace", Optional[str]),
            ("resources", Dict[str, Any]),
        ],
    )
):
    """Encapsulates configuration that can be applied to a K8s job running Dagster code.
    Can be persisted on a PipelineRun at run submission time based on metadata from the
    code location and then included in the job's configuration at run launch time or step
    launch time."""

    def __new__(
        cls,
        image_pull_policy: Optional[str] = None,
        image_pull_secrets: Optional[List[Dict[str, str]]] = None,
        service_account_name: Optional[str] = None,
        env_config_maps: Optional[List[str]] = None,
        env_secrets: Optional[List[str]] = None,
        env_vars: Optional[List[str]] = None,
        volume_mounts: Optional[List[Dict[str, Any]]] = None,
        volumes: Optional[List[Dict[str, Any]]] = None,
        labels: Optional[Dict[str, str]] = None,
        namespace: Optional[str] = None,
        resources: Optional[Dict[str, Any]] = None,
    ):
        return super(K8sContainerContext, cls).__new__(
            cls,
            image_pull_policy=check.opt_str_param(image_pull_policy, "image_pull_policy"),
            image_pull_secrets=check.opt_list_param(image_pull_secrets, "image_pull_secrets"),
            service_account_name=check.opt_str_param(service_account_name, "service_account_name"),
            env_config_maps=check.opt_list_param(env_config_maps, "env_config_maps"),
            env_secrets=check.opt_list_param(env_secrets, "env_secrets"),
            env_vars=check.opt_list_param(env_vars, "env_vars"),
            volume_mounts=[
                k8s_snake_case_dict(kubernetes.client.V1VolumeMount, mount)
                for mount in check.opt_list_param(volume_mounts, "volume_mounts")
            ],
            volumes=[
                k8s_snake_case_dict(kubernetes.client.V1Volume, volume)
                for volume in check.opt_list_param(volumes, "volumes")
            ],
            labels=check.opt_dict_param(labels, "labels"),
            namespace=check.opt_str_param(namespace, "namespace"),
            resources=check.opt_dict_param(resources, "resources"),
        )

    def merge(self, other: "K8sContainerContext") -> "K8sContainerContext":
        # Lists of attributes that can be combined are combined, scalar values are replaced
        # prefering the passed in container context
        return K8sContainerContext(
            image_pull_policy=(
                other.image_pull_policy if other.image_pull_policy else self.image_pull_policy
            ),
            image_pull_secrets=_dedupe_list(other.image_pull_secrets + self.image_pull_secrets),
            service_account_name=(
                other.service_account_name
                if other.service_account_name
                else self.service_account_name
            ),
            env_config_maps=_dedupe_list(other.env_config_maps + self.env_config_maps),
            env_secrets=_dedupe_list(other.env_secrets + self.env_secrets),
            env_vars=_dedupe_list(other.env_vars + self.env_vars),
            volume_mounts=_dedupe_list(other.volume_mounts + self.volume_mounts),
            volumes=_dedupe_list(other.volumes + self.volumes),
            labels=merge_dicts(other.labels, self.labels),
            namespace=other.namespace if other.namespace else self.namespace,
            resources=other.resources if other.resources else self.resources,
        )

    def get_environment_dict(self) -> Dict[str, str]:
        parsed_env_var_tuples = [parse_env_var(env_var) for env_var in self.env_vars]
        return {env_var_tuple[0]: env_var_tuple[1] for env_var_tuple in parsed_env_var_tuples}

    @staticmethod
    def create_for_run(
        pipeline_run: PipelineRun, run_launcher: Optional["K8sRunLauncher"]
    ) -> "K8sContainerContext":
        context = K8sContainerContext()

        if run_launcher:
            context = context.merge(
                K8sContainerContext(
                    image_pull_policy=run_launcher.image_pull_policy,
                    image_pull_secrets=run_launcher.image_pull_secrets,
                    service_account_name=run_launcher.service_account_name,
                    env_config_maps=run_launcher.env_config_maps,
                    env_secrets=run_launcher.env_secrets,
                    env_vars=run_launcher.env_vars,
                    volume_mounts=run_launcher.volume_mounts,
                    volumes=run_launcher.volumes,
                    labels=run_launcher.labels,
                    namespace=run_launcher.job_namespace,
                    resources=run_launcher.resources,
                )
            )

        run_container_context = (
            pipeline_run.pipeline_code_origin.repository_origin.container_context
            if pipeline_run.pipeline_code_origin
            else None
        )

        if not run_container_context:
            return context

        return context.merge(K8sContainerContext.create_from_config(run_container_context))

    @staticmethod
    def create_from_config(run_container_context) -> "K8sContainerContext":
        run_k8s_container_context = (
            run_container_context.get("k8s", {}) if run_container_context else {}
        )

        if not run_k8s_container_context:
            return K8sContainerContext()

        processed_container_context = process_config(
            DagsterK8sJobConfig.config_type_container_context(), run_k8s_container_context
        )

        if not processed_container_context.success:
            raise DagsterInvalidConfigError(
                "Errors while parsing k8s container context",
                processed_container_context.errors,
                run_k8s_container_context,
            )

        processed_context_value = cast(Dict, processed_container_context.value)

        return K8sContainerContext(
            image_pull_policy=processed_context_value.get("image_pull_policy"),
            image_pull_secrets=processed_context_value.get("image_pull_secrets"),
            service_account_name=processed_context_value.get("service_account_name"),
            env_config_maps=processed_context_value.get("env_config_maps"),
            env_secrets=processed_context_value.get("env_secrets"),
            env_vars=processed_context_value.get("env_vars"),
            volume_mounts=processed_context_value.get("volume_mounts"),
            volumes=processed_context_value.get("volumes"),
            labels=processed_context_value.get("labels"),
            namespace=processed_context_value.get("namespace"),
            resources=processed_context_value.get("resources"),
        )

    def get_k8s_job_config(self, job_image, run_launcher) -> DagsterK8sJobConfig:
        return DagsterK8sJobConfig(
            job_image=job_image if job_image else run_launcher.job_image,
            dagster_home=run_launcher.dagster_home,
            image_pull_policy=self.image_pull_policy,
            image_pull_secrets=self.image_pull_secrets,
            service_account_name=self.service_account_name,
            instance_config_map=run_launcher.instance_config_map,
            postgres_password_secret=run_launcher.postgres_password_secret,
            env_config_maps=self.env_config_maps,
            env_secrets=self.env_secrets,
            env_vars=self.env_vars,
            volume_mounts=self.volume_mounts,
            volumes=self.volumes,
            labels=self.labels,
            resources=self.resources,
        )
