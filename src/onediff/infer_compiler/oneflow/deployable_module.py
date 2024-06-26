import types
import torch
import oneflow as flow

from ..transform.manager import transform_mgr
from ..utils.oneflow_exec_mode import oneflow_exec_mode, oneflow_exec_mode_enabled
from ..utils.args_tree_util import input_output_processor
from ..utils.log_utils import logger
from ..utils.param_utils import parse_device, check_device
from ..utils.graph_management_utils import graph_file_management
from ..utils.online_quantization_utils import quantize_and_deploy_wrapper
from ..utils.options import OneflowCompileOptions
from ..deployable_module import DeployableModule

from .utils import handle_deployable_exception, get_mixed_dual_module, get_oneflow_graph


class OneflowDeployableModule(DeployableModule):
    def __init__(
        self, torch_module, oneflow_module, dynamic=True, options=None,
    ):
        torch.nn.Module.__init__(self)
        object.__setattr__(
            self,
            "_deployable_module_model",
            get_mixed_dual_module(torch_module.__class__)(torch_module, oneflow_module),
        )
        object.__setattr__(self, "_modules", torch_module._modules)
        object.__setattr__(self, "_torch_module", torch_module)
        self._deployable_module_enable_dynamic = dynamic
        self._deployable_module_quant_config = None
        self._deployable_module_options = (
            options if options is not None else OneflowCompileOptions()
        )
        self._deployable_module_dpl_graph = None
        self._is_raw_deployable_module = True
        self._load_graph_first_run = True

    @classmethod
    def from_existing(cls, existing_module, dynamic=True, options=None):
        torch_module = existing_module._deployable_module_model._torch_module
        oneflow_module = existing_module._deployable_module_model._oneflow_module
        instance = cls(torch_module, oneflow_module, dynamic, options)
        instance._deployable_module_dpl_graph = None
        if hasattr(existing_module, "_deployable_module_dpl_graph"):
            instance._deployable_module_dpl_graph = (
                existing_module._deployable_module_dpl_graph
            )
        instance._load_graph_first_run = existing_module._load_graph_first_run
        instance._deployable_module_input_count = (
            existing_module._deployable_module_input_count
        )
        instance._deployable_module_quant_config = (
            existing_module._deployable_module_quant_config
        )

        return instance

    def get_graph(self):
        if self._deployable_module_dpl_graph is not None:
            return self._deployable_module_dpl_graph
        self._deployable_module_dpl_graph = get_oneflow_graph(
            self._deployable_module_model.oneflow_module,
            self._deployable_module_options.max_cached_graph_size,
            self._deployable_module_enable_dynamic,
        )
        # Enable debug mode
        if transform_mgr.debug_mode:
            self._deployable_module_dpl_graph.debug(0)
        if self._deployable_module_options.debug_level > 0:
            self._deployable_module_dpl_graph.debug(
                self._deployable_module_options.debug_level
            )
        return self._deployable_module_dpl_graph

    @input_output_processor
    @handle_deployable_exception
    @graph_file_management
    def apply_model(self, *args, **kwargs):
        if self._deployable_module_options.use_graph:
            dpl_graph = self.get_graph()
            with oneflow_exec_mode():
                output = dpl_graph(*args, **kwargs)
        else:
            with oneflow_exec_mode():
                output = self._deployable_module_model.oneflow_module.apply_model(
                    *args, **kwargs
                )
        return output

    @quantize_and_deploy_wrapper
    @input_output_processor
    @handle_deployable_exception
    @graph_file_management
    def forward(self, *args, **kwargs):
        if self._deployable_module_options.use_graph:
            dpl_graph = self.get_graph()
            with oneflow_exec_mode():
                output = dpl_graph(*args, **kwargs)
        else:
            with oneflow_exec_mode():
                output = self._deployable_module_model.oneflow_module(*args, **kwargs)
        return output

    def to(self, *args, **kwargs):
        if self._deployable_module_dpl_graph is None:
            self._deployable_module_model.to(*args, **kwargs)
            return self

        # assert the target device is same as graph device
        target_device = parse_device(args, kwargs)
        if (
            target_device is not None
            and len(self._deployable_module_dpl_graph._blocks) > 0
        ):
            current_device = next(self._deployable_module_dpl_graph._state()).device
            if not check_device(current_device, target_device):
                raise RuntimeError(
                    f"After graph built, the device of graph can't be modified, current device: {current_device}, target device: {target_device}"
                )
        self._deployable_module_model.to(*args, **kwargs)
        return self

    # TODO(): Just for transformers VAE decoder
    @input_output_processor
    @handle_deployable_exception
    @graph_file_management
    def decode(self, *args, **kwargs):
        if self._deployable_module_options.use_graph:

            def _build(graph, *args, **kwargs):
                return graph.model.decode(*args, **kwargs)

            dpl_graph = self.get_graph()
            dpl_graph.build = types.MethodType(_build, dpl_graph)
            with oneflow_exec_mode():
                output = dpl_graph(*args, **kwargs)
        else:
            with oneflow_exec_mode():
                output = self._deployable_module_model.oneflow_module.decode(
                    *args, **kwargs
                )
        return output

    def __getattr__(self, name):
        return getattr(self._deployable_module_model, name)

    def load_graph(self, file_path, device=None, run_warmup=True):
        self.get_graph().load_graph(file_path, device, run_warmup)

    def save_graph(self, file_path):
        self.get_graph().save_graph(file_path)

    def extra_repr(self) -> str:
        return self._deployable_module_model.extra_repr()

    def set_graph_file(self, file_path: str) -> None:
        """ Sets the path of the graph file.

        If the new file path is different from the old one, clears old graph data.

        Args:
            `file_path` (str): The path of the graph file.
        """
        old_file_path = self.get_graph_file()
        if file_path and old_file_path == file_path:
            return
        self._deployable_module_options.graph_file = file_path
        self._clear_old_graph()

    def _clear_old_graph(self):
        self._load_graph_first_run = True
        self._deployable_module_dpl_graph = None
        del self._deployable_module_model.oneflow_module

    def get_graph_file(self):
        return self._deployable_module_options.graph_file

    def apply_online_quant(self, quant_config):
        """
        Applies the provided quantization configuration for online use.

        Args:
            quant_config (QuantizationConfig): The quantization configuration to apply.

        Example:
            >>> from onediff_quant.quantization import QuantizationConfig
            >>> quant_config = QuantizationConfig.from_settings(
            ...     quantize_conv=True,
            ...     quantize_linear=True,
            ...     conv_mae_threshold=0.005,
            ...     linear_mae_threshold=0.005,
            ...     conv_compute_density_threshold=300,
            ...     linear_compute_density_threshold=100,
            ...     cache_dir=args.cache_dir)
            >>> model.apply_online_quant(quant_config)
        """
        self._deployable_module_quant_config = quant_config
