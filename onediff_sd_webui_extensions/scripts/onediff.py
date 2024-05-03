import os
import warnings
import gradio as gr
from pathlib import Path
from typing import Union, Dict
import modules.scripts as scripts
import modules.shared as shared
from modules.sd_models import select_checkpoint
from modules.processing import process_images

from compile_ldm import compile_ldm_unet, SD21CompileCtx
from compile_sgm import compile_sgm_unet
from compile_vae import VaeCompileCtx
from onediff_lora import HijackLoraActivate

from onediff.infer_compiler.utils.log_utils import logger
from onediff.infer_compiler.utils.env_var import parse_boolean_from_env
from onediff.optimization.quant_optimizer import (
    quantize_model,
    varify_can_use_quantization,
)
from onediff import __version__ as onediff_version
from oneflow import __version__ as oneflow_version

"""oneflow_compiled UNetModel"""
compiled_unet = None
compiled_ckpt_name = None


def generate_graph_path(ckpt_name: str, model_name: str) -> str:
    base_output_dir = shared.opts.outdir_samples or shared.opts.outdir_txt2img_samples
    save_ckpt_graphs_path = os.path.join(base_output_dir, "graphs", ckpt_name)
    os.makedirs(save_ckpt_graphs_path, exist_ok=True)

    file_name = f"{model_name}_graph_{onediff_version}_oneflow_{oneflow_version}"

    graph_file_path = os.path.join(save_ckpt_graphs_path, file_name)

    return graph_file_path


def get_calibrate_info(filename: str) -> Union[None, Dict]:
    calibration_path = Path(select_checkpoint().filename).parent / filename
    if not calibration_path.exists():
        return None

    logger.info(f"Got calibrate info at {str(calibration_path)}")
    calibrate_info = {}
    with open(calibration_path, "r") as f:
        for line in f.readlines():
            line = line.strip()
            items = line.split(" ")
            calibrate_info[items[0]] = [
                float(items[1]),
                int(items[2]),
                [float(x) for x in items[3].split(",")],
            ]
    return calibrate_info


def compile_unet(
    unet_model, quantization=False, *, use_graph=True, options={},
):
    from ldm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelLDM
    from sgm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelSGM

    if isinstance(unet_model, UNetModelLDM):
        compiled_unet = compile_ldm_unet(
            unet_model, use_graph=use_graph, options=options
        )
    elif isinstance(unet_model, UNetModelSGM):
        compiled_unet = compile_sgm_unet(
            unet_model, use_graph=use_graph, options=options
        )
    else:
        warnings.warn(
            f"Unsupported model type: {type(unet_model)} for compilation , skip",
            RuntimeWarning,
        )
        compiled_unet = unet_model
    if quantization:
        calibrate_info = get_calibrate_info(
            f"{Path(select_checkpoint().filename).stem}_sd_calibrate_info.txt"
        )
        compiled_unet = quantize_model(
            compiled_unet, inplace=False, calibrate_info=calibrate_info
        )
    return compiled_unet


class UnetCompileCtx(object):
    """The unet model is stored in a global variable.
    The global variables need to be replaced with compiled_unet before process_images is run,
    and then the original model restored so that subsequent reasoning with onediff disabled meets expectations.
    """

    def __enter__(self):
        self._original_model = shared.sd_model.model.diffusion_model
        global compiled_unet
        shared.sd_model.model.diffusion_model = compiled_unet

    def __exit__(self, exc_type, exc_val, exc_tb):
        shared.sd_model.model.diffusion_model = self._original_model
        return False


class Script(scripts.Script):
    current_type = None

    def title(self):
        return "onediff_diffusion_model"

    def ui(self, is_img2img):
        """this function should create gradio UI elements. See https://gradio.app/docs/#components
        The return value should be an array of all components that are used in processing.
        Values of those returned components will be passed to run() and process() functions.
        """
        if not varify_can_use_quantization():
            ret = gr.HTML(
                """
                    <div style="padding: 20px; border: 1px solid #e0e0e0; border-radius: 5px; background-color: #f9f9f9;">
                        <div style="font-size: 18px; font-weight: bold; margin-bottom: 15px; color: #31708f;">
                            Hints Message
                        </div>
                        <div style="padding: 10px; border: 1px solid #31708f; border-radius: 5px; background-color: #f9f9f9;">
                            Hints: Enterprise function is not supported on your system.
                        </div>
                        <p style="margin-top: 15px;">
                            If you need Enterprise Level Support for your system or business, please send an email to 
                            <a href="mailto:business@siliconflow.com" style="color: #31708f; text-decoration: none;">business@siliconflow.com</a>.
                            <br>
                            Tell us about your use case, deployment scale, and requirements.
                        </p>
                        <p>
                            <strong>GitHub Issue:</strong>
                            <a href="https://github.com/siliconflow/onediff/issues" style="color: #31708f; text-decoration: none;">https://github.com/siliconflow/onediff/issues</a>
                        </p>
                    </div>
                    """
            )

        else:
            ret = gr.components.Checkbox(label="Model Quantization(int8) Speed Up")
        return [ret]

    def show(self, is_img2img):
        return True

    def need_compile(self, model):
        recompile = False

        def get_model_type(model):
            return {
                "is_sdxl": getattr(model, 'is_sdxl', False),
                "is_sd2": getattr(model, 'is_sd2', False),
                "is_sd1": getattr(model, 'is_sd1', False),
                "is_ssd": getattr(model, 'is_ssd', False),
            }

        if self.current_type == None:
            recompile = True
        else:
            for key, v in self.current_type.items():
                if v != getattr(model, key, False):
                    recompile = True
                    break

        if recompile == True:
            self.current_type = get_model_type(model)
        elif parse_boolean_from_env("ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION", "1"):
            warnings.warn(
                f"If you want to reuse the compiled graph, please set environ var `ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION` as '0', or the compiled graph will work incorrectly.",
                RuntimeWarning,
            )
        return recompile

    def torch_gc(self):
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()

    def run(self, p, quantization=False):
        global compiled_unet, compiled_ckpt_name
        current_checkpoint = shared.opts.sd_model_checkpoint
        original_diffusion_model = shared.sd_model.model.diffusion_model

        ckpt_name = (
            current_checkpoint + "_quantized" if quantization else current_checkpoint
        )

        if (
            quantization
            and ckpt_name != compiled_ckpt_name
            or self.need_compile(shared.sd_model)
        ):
            compile_options = {}

            del compiled_unet
            self.torch_gc()

            compiled_unet = compile_unet(
                original_diffusion_model,
                quantization=quantization,
                options=compile_options,
            )
            compiled_ckpt_name = ckpt_name
            
            self.torch_gc()
        else:
            logger.info(
                f"Model {current_checkpoint} has same sd type of graph type {self.current_type}, skip compile"
            )

        with UnetCompileCtx(), VaeCompileCtx(), SD21CompileCtx(), HijackLoraActivate():
            proc = process_images(p)
        return proc
