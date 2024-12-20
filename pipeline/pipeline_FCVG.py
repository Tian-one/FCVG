# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

import numpy as np
import PIL.Image
import torch
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
from models.controlnext_vid_svd import ControlNeXtSDVModel

from diffusers.image_processor import VaeImageProcessor
from diffusers.models import AutoencoderKLTemporalDecoder, UNetSpatioTemporalConditionModel
from diffusers.utils import BaseOutput, logging
from diffusers.utils.torch_utils import randn_tensor
from diffusers.pipelines.pipeline_utils import DiffusionPipeline
from models.unet_spatio_temporal_condition_controlnext import UNetSpatioTemporalConditionControlNeXtModel
from utils.scheduling_euler_discrete_karras_fix import EulerDiscreteScheduler
#from diffusers.pipelines.utils import PIL_INTERPOLATION, BaseOutput, logging
from diffusers.pipelines.stable_video_diffusion.pipeline_stable_video_diffusion import StableVideoDiffusionPipeline


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

def _get_add_time_ids(
        noise_aug_strength,
        dtype,
        batch_size,
        fps=4,
        motion_bucket_id=128,
        unet=None,
    ):
        add_time_ids = [fps, motion_bucket_id, noise_aug_strength]

        passed_add_embed_dim = unet.config.addition_time_embed_dim * len(add_time_ids)
        expected_add_embed_dim = unet.add_embedding.linear_1.in_features

        if expected_add_embed_dim != passed_add_embed_dim:
            raise ValueError(
                f"Model expects an added time embedding vector of length {expected_add_embed_dim}, but a vector of {passed_add_embed_dim} was created. The model has an incorrect config. Please check `unet.config.time_embedding_type` and `text_encoder_2.config.projection_dim`."
            )

        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        # add_time_ids = add_time_ids.repeat(batch_size * num_videos_per_prompt, 1)


        return add_time_ids


def _append_dims(x, target_dims):
    """Appends dimensions to the end of a tensor until it has target_dims dimensions."""
    dims_to_append = target_dims - x.ndim
    if dims_to_append < 0:
        raise ValueError(f"input has {x.ndim} dims but target_dims is {target_dims}, which is less")
    return x[(...,) + (None,) * dims_to_append]


def tensor2vid(video: torch.Tensor, processor, output_type="np"):
    # Based on:
    # https://github.com/modelscope/modelscope/blob/1509fdb973e5871f37148a4b5e5964cafd43e64d/modelscope/pipelines/multi_modal/text_to_video_synthesis_pipeline.py#L78

    batch_size, channels, num_frames, height, width = video.shape
    outputs = []
    for batch_idx in range(batch_size):
        batch_vid = video[batch_idx].permute(1, 0, 2, 3)
        batch_output = processor.postprocess(batch_vid, output_type)

        outputs.append(batch_output)

    return outputs


@dataclass
class StableVideoDiffusionPipelineOutput(BaseOutput):
    r"""
    Output class for zero-shot text-to-video pipeline.

    Args:
        frames (`[List[PIL.Image.Image]`, `np.ndarray`]):
            List of denoised PIL images of length `batch_size` or NumPy array of shape `(batch_size, height, width,
            num_channels)`.
    """

    frames: Union[List[PIL.Image.Image], np.ndarray]


class StableVideoDiffusionPipelineControlNeXtReverse(DiffusionPipeline):
    r"""
    Pipeline to generate video from an input image using Stable Video Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) model to encode and decode images to and from latent representations.
        image_encoder ([`~transformers.CLIPVisionModelWithProjection`]):
            Frozen CLIP image-encoder ([laion/CLIP-ViT-H-14-laion2B-s32B-b79K](https://huggingface.co/laion/CLIP-ViT-H-14-laion2B-s32B-b79K)).
        unet ([`UNetSpatioTemporalConditionModel`]):
            A `UNetSpatioTemporalConditionModel` to denoise the encoded image latents.
        scheduler ([`EulerDiscreteScheduler`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents.
        feature_extractor ([`~transformers.CLIPImageProcessor`]):
            A `CLIPImageProcessor` to extract features from generated images.
    """

    model_cpu_offload_seq = "image_encoder->unet->vae"
    _callback_tensor_inputs = ["latents"]

    def __init__(
        self,
        vae: AutoencoderKLTemporalDecoder,
        image_encoder: CLIPVisionModelWithProjection,
        unet: UNetSpatioTemporalConditionControlNeXtModel,
        controlnext: ControlNeXtSDVModel,
        scheduler: EulerDiscreteScheduler,
        feature_extractor: CLIPImageProcessor,
    ):
        super().__init__()
    
        self.register_modules(
            vae=vae,
            image_encoder=image_encoder,
            controlnext=controlnext,
            unet=unet,
            scheduler=scheduler,
            feature_extractor=feature_extractor,
        )
        
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)

    
    def _encode_image(self, image, device, num_videos_per_prompt, do_classifier_free_guidance):
        dtype = next(self.image_encoder.parameters()).dtype

        if not isinstance(image, torch.Tensor):
            image = self.image_processor.pil_to_numpy(image)
            image = self.image_processor.numpy_to_pt(image)

            # We normalize the image before resizing to match with the original implementation.
            # Then we unnormalize it after resizing.
            image = image * 2.0 - 1.0
            image = _resize_with_antialiasing(image, (224, 224))
            image = (image + 1.0) / 2.0

            # Normalize the image with for CLIP input
            image = self.feature_extractor(
                images=image,
                do_normalize=True,
                do_center_crop=False,
                do_resize=False,
                do_rescale=False,
                return_tensors="pt",
            ).pixel_values

        image = image.to(device=device, dtype=dtype)
        image_embeddings = self.image_encoder(image).image_embeds
        image_embeddings = image_embeddings.unsqueeze(1)

        # duplicate image embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = image_embeddings.shape
        image_embeddings = image_embeddings.repeat(1, num_videos_per_prompt, 1)
        image_embeddings = image_embeddings.view(bs_embed * num_videos_per_prompt, seq_len, -1)

        if do_classifier_free_guidance:
            negative_image_embeddings = torch.zeros_like(image_embeddings)

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            image_embeddings = torch.cat([negative_image_embeddings, image_embeddings])

        return image_embeddings
    

    def _encode_vae_image(
        self,
        image: torch.Tensor,
        device,
        num_videos_per_prompt,
        do_classifier_free_guidance,
    ):
        image = image.to(device=device)
        image_latents = self.vae.encode(image).latent_dist.mode()

        if do_classifier_free_guidance:
            negative_image_latents = torch.zeros_like(image_latents)

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            image_latents = torch.cat([negative_image_latents, image_latents])

        # duplicate image_latents for each generation per prompt, using mps friendly method
        image_latents = image_latents.repeat(num_videos_per_prompt, 1, 1, 1)

        return image_latents

    def _get_add_time_ids(
        self,
        fps,
        motion_bucket_id,
        noise_aug_strength,
        dtype,
        batch_size,
        num_videos_per_prompt,
        do_classifier_free_guidance,
    ):
        add_time_ids = [fps, motion_bucket_id, noise_aug_strength]

        passed_add_embed_dim = self.unet.config.addition_time_embed_dim * len(add_time_ids)
        expected_add_embed_dim = self.unet.add_embedding.linear_1.in_features

        if expected_add_embed_dim != passed_add_embed_dim:
            raise ValueError(
                f"Model expects an added time embedding vector of length {expected_add_embed_dim}, but a vector of {passed_add_embed_dim} was created. The model has an incorrect config. Please check `unet.config.time_embedding_type` and `text_encoder_2.config.projection_dim`."
            )

        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        add_time_ids = add_time_ids.repeat(batch_size * num_videos_per_prompt, 1)

        if do_classifier_free_guidance:
            add_time_ids = torch.cat([add_time_ids, add_time_ids])

        return add_time_ids

    def decode_latents(self, latents, num_frames, decode_chunk_size=14):
        # [batch, frames, channels, height, width] -> [batch*frames, channels, height, width]
        latents = latents.flatten(0, 1)

        latents = 1 / self.vae.config.scaling_factor * latents

        accepts_num_frames = "num_frames" in set(inspect.signature(self.vae.forward).parameters.keys())

        # decode decode_chunk_size frames at a time to avoid OOM
        frames = []
        for i in range(0, latents.shape[0], decode_chunk_size):
            num_frames_in = latents[i : i + decode_chunk_size].shape[0]
            decode_kwargs = {}
            # if accepts_num_frames:
            #     # we only pass num_frames_in if it's expected
            #     decode_kwargs["num_frames"] = num_frames_in
            decode_kwargs["num_frames"] = num_frames_in
            frame = self.vae.decode(latents[i : i + decode_chunk_size], **decode_kwargs).sample
            frames.append(frame)
        frames = torch.cat(frames, dim=0)

        # [batch*frames, channels, height, width] -> [batch, channels, frames, height, width]
        frames = frames.reshape(-1, num_frames, *frames.shape[1:]).permute(0, 2, 1, 3, 4)

        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        frames = frames.float()
        return frames

    def check_inputs(self, image, height, width):
        if (
            not isinstance(image, torch.Tensor)
            and not isinstance(image, PIL.Image.Image)
            and not isinstance(image, list)
        ):
            raise ValueError(
                "`image` has to be of type `torch.FloatTensor` or `PIL.Image.Image` or `List[PIL.Image.Image]` but is"
                f" {type(image)}"
            )

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

    def prepare_latents(
        self,
        batch_size,
        num_frames,
        num_channels_latents,
        height,
        width,
        dtype,
        device,
        generator,
        latents=None,
    ):
        shape = (
            batch_size,
            num_frames,
            num_channels_latents // 2,
            height // self.vae_scale_factor,
            width // self.vae_scale_factor,
        )
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            latents = randn_tensor(shape, generator=generator, device=device, dtype=dtype)
        else:
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @property
    def guidance_scale(self):
        return self._guidance_scale

    # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
    # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
    # corresponds to doing no classifier free guidance.
    @property
    def do_classifier_free_guidance(self):
        return self._guidance_scale >= 1 and self.unet.config.time_cond_proj_dim is None

    @property
    def num_timesteps(self):
        return self._num_timesteps

    @torch.no_grad()
    def multidiffusion_step(self, latents, t, 
                    image1_embeddings, 
                    image2_embeddings, 
                    image1_latents,
                    image2_latents,
                    controlnext_condition1,
                    controlnext_condition2,
                    added_time_ids, 
                    avg_weight,
                    do_classifier_free_guidance,
                    control_weight=1.0,
    ):
        # expand the latents if we are doing classifier free guidance
        latents1 = latents
        latents2 = torch.flip(latents, (1,))
        latent_model_input1 = torch.cat([latents1] * 2) if do_classifier_free_guidance else latents1
        latent_model_input1 = self.scheduler.scale_model_input(latent_model_input1, t)

        latent_model_input2 = torch.cat([latents2] * 2) if do_classifier_free_guidance else latents2
        latent_model_input2= self.scheduler.scale_model_input(latent_model_input2, t)


        controlnext_output1 = self.controlnext(controlnext_condition1, t,)
        if do_classifier_free_guidance:
            N = controlnext_output1['output'].shape[0]
            controlnext_output1['scale'] = torch.tensor(controlnext_output1['scale']).to(latent_model_input1).repeat(N)[:, None, None, None]
            controlnext_output1['scale'][:N // 2] *= 0
        controlnext_output2 = self.controlnext(controlnext_condition2, t,)
        if do_classifier_free_guidance:
            N = controlnext_output2['output'].shape[0]
            controlnext_output2['scale'] = torch.tensor(controlnext_output2['scale']).to(latent_model_input1).repeat(N)[:, None, None, None]
            controlnext_output2['scale'][:N // 2] *= 0

        # Concatenate image_latents over channels dimention
        latent_model_input1 = torch.cat([latent_model_input1, image1_latents], dim=2)
        latent_model_input2 = torch.cat([latent_model_input2, image2_latents], dim=2)

        # predict the noise residual
        noise_pred1 = self.unet(
            latent_model_input1,
            t,
            encoder_hidden_states=image1_embeddings,
            added_time_ids=added_time_ids,
            conditional_controls=controlnext_output1,
            return_dict=False,
            control_weight=control_weight,
            )[0]
        
        noise_pred2 = self.unet(
            latent_model_input2,
            t,
            encoder_hidden_states=image2_embeddings,
            added_time_ids=added_time_ids,
            conditional_controls=controlnext_output2,
            return_dict=False,
            control_weight=control_weight,
            )[0]


        # perform guidance
        if do_classifier_free_guidance:
            noise_pred_uncond1, noise_pred_cond1 = noise_pred1.chunk(2)
            noise_pred1 = noise_pred_uncond1 + self.guidance_scale * (noise_pred_cond1 - noise_pred_uncond1)

            noise_pred_uncond2, noise_pred_cond2 = noise_pred2.chunk(2)
            noise_pred2 = noise_pred_uncond2 + self.guidance_scale * (noise_pred_cond2 - noise_pred_uncond2)

        noise_pred2 = torch.flip(noise_pred2, (1,))
        # print(noise_pred.shape, avg_weight.shape)
        noise_pred = avg_weight*noise_pred1+ (1-avg_weight)*noise_pred2
        return noise_pred

    @torch.no_grad()
    def __call__(
        self,
        image1: Union[PIL.Image.Image, List[PIL.Image.Image], torch.FloatTensor],
        image2: Union[PIL.Image.Image, List[PIL.Image.Image], torch.FloatTensor],
        controlnext_condition:Optional[torch.FloatTensor] = None,
        height: int = 576,
        width: int = 1024,
        num_frames: Optional[int] = None,
        num_inference_steps: int = 25,
        min_guidance_scale: float = 1.0,
        max_guidance_scale: float = 3.0,
        fps: int = 7,
        motion_bucket_id: int = 127,
        noise_aug_strength: int = 0.02,
        decode_chunk_size: Optional[int] = None,
        num_videos_per_prompt: Optional[int] = 1,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        callback_on_step_end: Optional[Callable[[int, int, Dict], None]] = None,
        callback_on_step_end_tensor_inputs: List[str] = ["latents"],
        return_dict: bool = True,
        batch_size=1,
        overlap=5,
        frames_per_batch = 14,
        noise_injection_steps: int = 0,
        noise_injection_ratio: float=0.0,
        control_weight: float=1.0,
    ):
        r"""
        The call function to the pipeline for generation.

        Args:
            image (`PIL.Image.Image` or `List[PIL.Image.Image]` or `torch.FloatTensor`):
                Image or images to guide image generation. If you provide a tensor, it needs to be compatible with
                [`CLIPImageProcessor`](https://huggingface.co/lambdalabs/sd-image-variations-diffusers/blob/main/feature_extractor/preprocessor_config.json).
            height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The width in pixels of the generated image.
            num_frames (`int`, *optional*):
                The number of video frames to generate. Defaults to 14 for `stable-video-diffusion-img2vid` and to 25 for `stable-video-diffusion-img2vid-xt`
            num_inference_steps (`int`, *optional*, defaults to 25):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference. This parameter is modulated by `strength`.
            min_guidance_scale (`float`, *optional*, defaults to 1.0):
                The minimum guidance scale. Used for the classifier free guidance with first frame.
            max_guidance_scale (`float`, *optional*, defaults to 3.0):
                The maximum guidance scale. Used for the classifier free guidance with last frame.
            fps (`int`, *optional*, defaults to 7):
                Frames per second. The rate at which the generated images shall be exported to a video after generation.
                Note that Stable Diffusion Video's UNet was micro-conditioned on fps-1 during training.
            motion_bucket_id (`int`, *optional*, defaults to 127):
                The motion bucket ID. Used as conditioning for the generation. The higher the number the more motion will be in the video.
            noise_aug_strength (`int`, *optional*, defaults to 0.02):
                The amount of noise added to the init image, the higher it is the less the video will look like the init image. Increase it for more motion.
            decode_chunk_size (`int`, *optional*):
                The number of frames to decode at a time. The higher the chunk size, the higher the temporal consistency
                between frames, but also the higher the memory consumption. By default, the decoder will decode all frames at once
                for maximal quality. Reduce `decode_chunk_size` to reduce memory usage.
            num_videos_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
                generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generated image. Choose between `PIL.Image` or `np.array`.
            callback_on_step_end (`Callable`, *optional*):
                A function that calls at the end of each denoising steps during the inference. The function is called
                with the following arguments: `callback_on_step_end(self: DiffusionPipeline, step: int, timestep: int,
                callback_kwargs: Dict)`. `callback_kwargs` will include a list of all tensors as specified by
                `callback_on_step_end_tensor_inputs`.
            callback_on_step_end_tensor_inputs (`List`, *optional*):
                The list of tensor inputs for the `callback_on_step_end` function. The tensors specified in the list
                will be passed as `callback_kwargs` argument. You will only be able to include variables listed in the
                `._callback_tensor_inputs` attribute of your pipeline class.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.

        Returns:
            [`~pipelines.stable_diffusion.StableVideoDiffusionPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.stable_diffusion.StableVideoDiffusionPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list of list with the generated frames.

        Examples:

        ```py
        from diffusers import StableVideoDiffusionPipeline
        from diffusers.utils import load_image, export_to_video

        pipe = StableVideoDiffusionPipeline.from_pretrained("stabilityai/stable-video-diffusion-img2vid-xt", torch_dtype=torch.float16, variant="fp16")
        pipe.to("cuda")

        image = load_image("https://lh3.googleusercontent.com/y-iFOHfLTwkuQSUegpwDdgKmOjRSTvPxat63dQLB25xkTs4lhIbRUFeNBWZzYf370g=s1200")
        image = image.resize((1024, 576))

        frames = pipe(image, num_frames=25, decode_chunk_size=8).frames[0]
        export_to_video(frames, "generated.mp4", fps=7)
        ```
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        num_frames = num_frames if num_frames is not None else self.unet.config.num_frames
        decode_chunk_size = decode_chunk_size if decode_chunk_size is not None else num_frames
        frames_per_batch = min(frames_per_batch, num_frames)

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(image1, height, width)
        self.check_inputs(image2, height, width)

        # 2. Define call parameters
        #if isinstance(image, PIL.Image.Image):
        #    batch_size = 1
        #elif isinstance(image, list):
        #    batch_size = len(image)
        #else:
        #    batch_size = image.shape[0]
        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = max_guidance_scale >= 1.0
        # 3. Encode input image
        image1_embeddings = self._encode_image(image1, device, num_videos_per_prompt, do_classifier_free_guidance)
        image2_embeddings = self._encode_image(image2, device, num_videos_per_prompt, do_classifier_free_guidance)

        # NOTE: Stable Diffusion Video was conditioned on fps - 1, which
        # is why it is reduced here.
        # See: https://github.com/Stability-AI/generative-models/blob/ed0997173f98eaf8f4edf7ba5fe8f15c6b877fd3/scripts/sampling/simple_video_sample.py#L188
        fps = fps - 1

        # 4. Encode input image using VAE
        image1 = self.image_processor.preprocess(image1, height=height, width=width).to(device)
        image2 = self.image_processor.preprocess(image2, height=height, width=width).to(device)
        noise = randn_tensor(image1.shape, generator=generator, device=image1.device, dtype=image1.dtype)
        image1 = image1 + noise_aug_strength * noise
        image2 = image2 + noise_aug_strength * noise

        needs_upcasting = (self.vae.dtype == torch.float16 or self.vae.dtype == torch.bfloat16)  and self.vae.config.force_upcast
        if needs_upcasting:
            self_vae_dtype = self.vae.dtype
            self.vae.to(dtype=torch.float32)
        # Repeat the image latents for each frame so we can concatenate them with the noise
        # image_latents [batch, channels, height, width] ->[batch, num_frames, channels, height, width]
        image1_latent = self._encode_vae_image(image1, device, num_videos_per_prompt, do_classifier_free_guidance)
        image1_latent = image1_latent.to(image1_embeddings.dtype)
        image1_latents = image1_latent.unsqueeze(1).repeat(1, num_frames, 1, 1, 1)

        image2_latent = self._encode_vae_image(image2, device, num_videos_per_prompt, do_classifier_free_guidance)
        image2_latent = image2_latent.to(image2_embeddings.dtype)
        image2_latents = image2_latent.unsqueeze(1).repeat(1, num_frames, 1, 1, 1)

        # cast back to fp16 if needed
        if needs_upcasting:
            self.vae.to(dtype=self_vae_dtype)


        #image_latents = torch.cat([image_latents] * 2) if do_classifier_free_guidance else image_latents
        
        # 5. Get Added Time IDs
        added_time_ids = self._get_add_time_ids(
            fps,
            motion_bucket_id,
            noise_aug_strength,
            image1_embeddings.dtype,
            batch_size,
            num_videos_per_prompt,
            do_classifier_free_guidance,
        )
        added_time_ids = added_time_ids.to(device)

        # 4. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # 5. Prepare latent variables
 
        num_channels_latents = self.unet.config.in_channels
        latents = self.prepare_latents(
            batch_size * num_videos_per_prompt,
            num_frames,
            num_channels_latents,
            height,
            width,
            image1_embeddings.dtype,
            device,
            generator,
            latents,
        )
        #prepare controlnext condition
        controlnext_condition = self.image_processor.preprocess(controlnext_condition, height=height, width=width)
        controlnext_condition = (controlnext_condition + 1.0) / 2
        controlnext_condition = controlnext_condition.unsqueeze(0)
        if do_classifier_free_guidance:
            controlnext_condition = torch.cat([controlnext_condition] * 2) 
        controlnext_condition = controlnext_condition.to(device, latents.dtype)
        controlnext_condition_all = controlnext_condition


        latents_all = latents
        
        # 7. Prepare guidance scale
        guidance_scale = torch.linspace(min_guidance_scale, max_guidance_scale, frames_per_batch).unsqueeze(0)
        guidance_scale = guidance_scale.to(device, latents.dtype)
        guidance_scale = guidance_scale.repeat(batch_size * num_videos_per_prompt, 1)
        guidance_scale = _append_dims(guidance_scale, latents.ndim)

        self._guidance_scale = guidance_scale
        w = torch.linspace(1, 0, frames_per_batch).unsqueeze(0).to(device, latents.dtype)
        w = w.repeat(batch_size*num_videos_per_prompt, 1)
        w = _append_dims(w, latents.ndim)

        # noise_aug_strength = 0.02 #"¯\_(ツ)_/¯
        # added_time_ids = _get_add_time_ids(
        #     noise_aug_strength,
        #     image_embeddings.dtype,
        #     batch_size,
        #     6,
        #     128,
        #     unet=self.unet,
        # )
        # if do_classifier_free_guidance:
        #     added_time_ids = torch.cat([added_time_ids] * 2) 
        # added_time_ids = added_time_ids.to(latents.device)

        
        # 8. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        self._num_timesteps = len(timesteps)
        noise_injection_step_threshold = int(num_inference_steps*noise_injection_ratio)
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                pred_tmp = torch.zeros_like(latents_all)
                counter = torch.zeros((latents.shape[0], num_frames, 1, 1, 1 )).to(device=latents.device)
                for batch, ind_start_idx in enumerate(range(0, num_frames-overlap, frames_per_batch-overlap)):
                    self.scheduler._step_index = None
                    if ind_start_idx + frames_per_batch > num_frames:
                        ind_start = num_frames - frames_per_batch
                    else:
                        ind_start = ind_start_idx
                    latents = latents_all[:,ind_start:ind_start+frames_per_batch].contiguous()

                    controlnext_condition1 = controlnext_condition_all[:,ind_start:ind_start+frames_per_batch].contiguous()
                    controlnext_condition1[:, 0, ...] = controlnext_condition_all[:, 0, ...]
                    controlnext_condition2 = torch.flip(controlnext_condition1, (1,))


                    image1_latent_batch = image1_latents[:,ind_start:ind_start+frames_per_batch].contiguous()
                    image2_latent_batch = image2_latents[:,ind_start:ind_start+frames_per_batch].contiguous()
                    noise_pred = self.multidiffusion_step(latents, t, 
                        image1_embeddings, image2_embeddings, 
                        image1_latent_batch, image2_latent_batch, controlnext_condition1, controlnext_condition2,
                        added_time_ids, w, do_classifier_free_guidance, control_weight
                    )

                    # expand the latents if we are doing classifier free guidance
                    # latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                    # latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

                    # controlnext_output = self.controlnext(
                    #     controlnext_condition,
                    #     t,
                    # )
                    # if do_classifier_free_guidance:
                    #     N = controlnext_output['output'].shape[0]
                    #     controlnext_output['scale'] = torch.tensor(controlnext_output['scale']).to(latent_model_input).repeat(N)[:, None, None, None]
                    #     controlnext_output['scale'][:N // 2] *= 0


                    # # Concatenate image_latents over channels dimention
                    # latent_model_input = torch.cat([latent_model_input, image_latents[:,ind_start:ind_start+frames_per_batch].contiguous()], dim=2)
                   
                    
                    # # predict the noise residual
                    # noise_pred = self.unet(
                    #     latent_model_input,
                    #     t,
                    #     encoder_hidden_states=image_embeddings,
                    #     added_time_ids=added_time_ids,
                    #     conditional_controls=controlnext_output,
                    #     return_dict=False,
                    # )[0]

                    # # perform guidance
                    # if do_classifier_free_guidance:
                    #     noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
                    #     noise_pred = noise_pred_uncond + self.guidance_scale * (noise_pred_cond - noise_pred_uncond)

                    # compute the previous noisy sample x_t -> x_t-1
                    latents = self.scheduler.step(noise_pred, t, latents).prev_sample

                    if i < noise_injection_step_threshold and noise_injection_steps > 0:
                        sigma_t = self.scheduler.sigmas[self.scheduler.step_index]
                        sigma_tm1 = self.scheduler.sigmas[self.scheduler.step_index+1]
                        sigma = torch.sqrt(sigma_t**2-sigma_tm1**2)
                        for j in range(noise_injection_steps):
                            noise = randn_tensor(latents.shape, device=latents.device, dtype=latents.dtype)
                            noise = noise * sigma
                            latents = latents + noise
                            noise_pred = self.multidiffusion_step(latents, t, 
                                image1_embeddings, image2_embeddings, 
                                image1_latent_batch, image2_latent_batch, controlnext_condition1, controlnext_condition2,
                                added_time_ids, w, do_classifier_free_guidance
                            )
                            # compute the previous noisy sample x_t -> x_t-1
                            latents = self.scheduler.step(noise_pred, t, latents).prev_sample
                    self.scheduler._step_index += 1

                    if callback_on_step_end is not None:
                        callback_kwargs = {}
                        for k in callback_on_step_end_tensor_inputs:
                            callback_kwargs[k] = locals()[k]
                        callback_outputs = callback_on_step_end(self, i, t, callback_kwargs)

                        latents = callback_outputs.pop("latents", latents)
                    
                    if ind_start == 0:
                        pred_tmp[:,ind_start:ind_start+frames_per_batch] += latents
                        counter[:,ind_start:ind_start+frames_per_batch] += 1
                    else:
                        pred_tmp[:,ind_start + 1 : ind_start+frames_per_batch] += latents[:, 1:, ...]
                        counter[:,ind_start + 1:ind_start+frames_per_batch] += 1
                pred_tmp /= counter
                latents_all = pred_tmp

                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
        latents = latents_all

        if not output_type == "latent":
            # cast back to fp16 if needed
            if needs_upcasting:
                self.vae.to(dtype=self_vae_dtype)
            frames = self.decode_latents(latents, num_frames, decode_chunk_size)
            frames = tensor2vid(frames, self.image_processor, output_type=output_type)
        else:
            frames = latents

        self.maybe_free_model_hooks()

        if not return_dict:
            return frames

        return StableVideoDiffusionPipelineOutput(frames=frames)


# resizing utils
# TODO: clean up later
def _resize_with_antialiasing(input, size, interpolation="bicubic", align_corners=True):
    
    if input.ndim == 3:
        input = input.unsqueeze(0)  # Add a batch dimension
        
    h, w = input.shape[-2:]
    factors = (h / size[0], w / size[1])

    # First, we have to determine sigma
    # Taken from skimage: https://github.com/scikit-image/scikit-image/blob/v0.19.2/skimage/transform/_warps.py#L171
    sigmas = (
        max((factors[0] - 1.0) / 2.0, 0.001),
        max((factors[1] - 1.0) / 2.0, 0.001),
    )

    # Now kernel size. Good results are for 3 sigma, but that is kind of slow. Pillow uses 1 sigma
    # https://github.com/python-pillow/Pillow/blob/master/src/libImaging/Resample.c#L206
    # But they do it in the 2 passes, which gives better results. Let's try 2 sigmas for now
    ks = int(max(2.0 * 2 * sigmas[0], 3)), int(max(2.0 * 2 * sigmas[1], 3))

    # Make sure it is odd
    if (ks[0] % 2) == 0:
        ks = ks[0] + 1, ks[1]

    if (ks[1] % 2) == 0:
        ks = ks[0], ks[1] + 1

    input = _gaussian_blur2d(input, ks, sigmas)

    output = torch.nn.functional.interpolate(input, size=size, mode=interpolation, align_corners=align_corners)
    return output


def _compute_padding(kernel_size):
    """Compute padding tuple."""
    # 4 or 6 ints:  (padding_left, padding_right,padding_top,padding_bottom)
    # https://pytorch.org/docs/stable/nn.html#torch.nn.functional.pad
    if len(kernel_size) < 2:
        raise AssertionError(kernel_size)
    computed = [k - 1 for k in kernel_size]

    # for even kernels we need to do asymmetric padding :(
    out_padding = 2 * len(kernel_size) * [0]

    for i in range(len(kernel_size)):
        computed_tmp = computed[-(i + 1)]

        pad_front = computed_tmp // 2
        pad_rear = computed_tmp - pad_front

        out_padding[2 * i + 0] = pad_front
        out_padding[2 * i + 1] = pad_rear

    return out_padding


def _filter2d(input, kernel):
    # prepare kernel
    b, c, h, w = input.shape
    tmp_kernel = kernel[:, None, ...].to(device=input.device, dtype=input.dtype)

    tmp_kernel = tmp_kernel.expand(-1, c, -1, -1)

    height, width = tmp_kernel.shape[-2:]

    padding_shape: list[int] = _compute_padding([height, width])
    input = torch.nn.functional.pad(input, padding_shape, mode="reflect")

    # kernel and input tensor reshape to align element-wise or batch-wise params
    tmp_kernel = tmp_kernel.reshape(-1, 1, height, width)
    input = input.view(-1, tmp_kernel.size(0), input.size(-2), input.size(-1))

    # convolve the tensor with the kernel.
    output = torch.nn.functional.conv2d(input, tmp_kernel, groups=tmp_kernel.size(0), padding=0, stride=1)

    out = output.view(b, c, h, w)
    return out


def _gaussian(window_size: int, sigma):
    if isinstance(sigma, float):
        sigma = torch.tensor([[sigma]])

    batch_size = sigma.shape[0]

    x = (torch.arange(window_size, device=sigma.device, dtype=sigma.dtype) - window_size // 2).expand(batch_size, -1)

    if window_size % 2 == 0:
        x = x + 0.5

    gauss = torch.exp(-x.pow(2.0) / (2 * sigma.pow(2.0)))

    return gauss / gauss.sum(-1, keepdim=True)


def _gaussian_blur2d(input, kernel_size, sigma):
    if isinstance(sigma, tuple):
        sigma = torch.tensor([sigma], dtype=input.dtype)
    else:
        sigma = sigma.to(dtype=input.dtype)

    ky, kx = int(kernel_size[0]), int(kernel_size[1])
    bs = sigma.shape[0]
    kernel_x = _gaussian(kx, sigma[:, 1].view(bs, 1))
    kernel_y = _gaussian(ky, sigma[:, 0].view(bs, 1))
    out_x = _filter2d(input, kernel_x[..., None, :])
    out = _filter2d(out_x, kernel_y[..., None])

    return out
