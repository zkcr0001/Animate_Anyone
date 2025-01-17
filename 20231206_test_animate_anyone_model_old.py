from diffusers import AutoencoderKL
from omegaconf import OmegaConf
from PIL import Image
import requests

from diffusers.models import UNet2DConditionModel
# from animatediff.models.animate_anyone_network import AnimateAnyoneModel
from animatediff.models.animate_anyone_network import UNet3DConditionModel
from animatediff.models.animate_anyone_network import PoseGuider3D
from animatediff.pipelines.pipeline_animation import AnimationPipeline
from sentence_transformers import SentenceTransformer
import torch.nn as nn
import torch
from einops import rearrange

class AnimateAnyoneModel(nn.Module):
    def __init__(self, VAE, CLIP, ReferenceNet, Pose_Guider3D, Unet_3D):
        super(AnimateAnyoneModel, self).__init__()

        self.VAE = VAE
        self.CLIP = CLIP
        self.ReferenceNet = ReferenceNet
        self.Pose_Guider3D = Pose_Guider3D
        self.Unet_3D = Unet_3D

    def forward(self, noise_sequence, reference_image_torch, reference_image_pil, pose_sequence, timesteps):
        # noise_sequence # [B, 4, F, H, W]
        # reference_image_torch # [B, C, H, W]
        # reference_image_pil # PIL image
        # pose_sequence # [B, F, C, H, W]
        reference_latents = self.VAE.encode(reference_image_torch).latent_dist.sample() # [B, 4, H // 8, W // 8]
        reference_prompt = self.CLIP.encode(reference_image_pil) # numpy (768,)
        reference_prompt = torch.from_numpy(reference_prompt).repeat(1, 77, 1).cuda() # torch.size([1, 77, 768])
        # TODO: should we pad zero, should we add classifier free guidance
        pose_sequence = rearrange(pose_sequence, "b f c h w -> b c f h w")
        # ======================
        # reference network
        # ======================
        attention_mask = None
        forward_upsample_size = False
        upsample_size = None
        # 1. time embedding
        timesteps = timesteps.expand(reference_latents.shape[0])
        t_emb = self.ReferenceNet.time_proj(timesteps)
        t_emb = t_emb.to(self.ReferenceNet.dtype)
        emb = self.ReferenceNet.time_embedding(t_emb)
        # 2. preprocess
        reference_latents = self.ReferenceNet.conv_in(reference_latents) # [B , 320, H // 8, W // 8])
        # concat
        # 3. down
        down_block_res_samples = (reference_latents,)
        for downsample_block in self.ReferenceNet.down_blocks:
            if hasattr(downsample_block, "has_cross_attention") and downsample_block.has_cross_attention:
                reference_latents, res_samples = downsample_block(
                    hidden_states=reference_latents,
                    temb=emb,
                    encoder_hidden_states=reference_prompt,
                    attention_mask=attention_mask,
                )
            else:
                reference_latents, res_samples = downsample_block(hidden_states=reference_latents, temb=emb)

            down_block_res_samples += res_samples
            print("reference shape:", reference_latents.shape)

        # 4. mid
        reference_latents = self.ReferenceNet.mid_block(
            reference_latents, emb, encoder_hidden_states=reference_prompt, attention_mask=attention_mask
        )
        print("reference shape:", reference_latents.shape)

        # 5. up
        for i, upsample_block in enumerate(self.ReferenceNet.up_blocks):
            is_final_block = i == len(self.ReferenceNet.up_blocks) - 1

            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]

            # if we have not reached the final block and need to forward the
            # upsample size, we do it here
            if not is_final_block and forward_upsample_size:
                upsample_size = down_block_res_samples[-1].shape[2:]

            if hasattr(upsample_block, "has_cross_attention") and upsample_block.has_cross_attention:
                reference_latents = upsample_block(
                    hidden_states=reference_latents,
                    temb=emb,
                    res_hidden_states_tuple=res_samples,
                    encoder_hidden_states=reference_prompt,
                    upsample_size=upsample_size,
                    attention_mask=attention_mask,
                )
            else:
                reference_latents = upsample_block(
                    hidden_states=reference_latents, temb=emb, res_hidden_states_tuple=res_samples, upsample_size=upsample_size
                )
            print("reference shape:", reference_latents.shape)
        # 6. post-process
        reference_latents = self.ReferenceNet.conv_norm_out(reference_latents)
        print("reference shape:", reference_latents.shape)
        reference_latents = self.ReferenceNet.conv_act(reference_latents)
        print("reference shape:", reference_latents.shape)
        reference_latents = self.ReferenceNet.conv_out(reference_latents)
        print("reference shape:", reference_latents.shape)

        # ======================
        # reference network 3D unet
        # remember to add pose_guidance sequence
        # ======================

        pose_guidance_sequence = self.Pose_Guider3D(pose_sequence) # [B, 320, F, H // 8, W // 8]

        attention_mask = None
        forward_upsample_size = False
        upsample_size = None        
        # 1. time embedding
        # this is the same for ReferenceNet and Unet_3D
        # 2. preprocess
        noise_sequence = self.Unet_3D.conv_in(noise_sequence) # [B , 320, H // 8, W // 8])
        # add reference latents and pose_guidance_sequence
        noise_sequence += pose_guidance_sequence
        # 3. down
        down_block_res_samples = (noise_sequence,)
        for downsample_block in self.Unet_3D.down_blocks:
            if hasattr(downsample_block, "has_cross_attention") and downsample_block.has_cross_attention:
                noise_sequence, res_samples = downsample_block(
                    hidden_states=noise_sequence,
                    temb=emb,
                    encoder_hidden_states=reference_prompt,
                    attention_mask=attention_mask,
                )
            else:
                noise_sequence, res_samples = downsample_block(hidden_states=noise_sequence, temb=emb)

            down_block_res_samples += res_samples
            print("noise sequence shape:", noise_sequence.shape)

        # 4. mid
        noise_sequence = self.Unet_3D.mid_block(
            noise_sequence, emb, encoder_hidden_states=reference_prompt, attention_mask=attention_mask
        )
        print("noise sequence shape:", noise_sequence.shape)

        # 5. up
        for i, upsample_block in enumerate(self.Unet_3D.up_blocks):
            is_final_block = i == len(self.Unet_3D.up_blocks) - 1

            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]

            # if we have not reached the final block and need to forward the
            # upsample size, we do it here
            if not is_final_block and forward_upsample_size:
                upsample_size = down_block_res_samples[-1].shape[2:]

            if hasattr(upsample_block, "has_cross_attention") and upsample_block.has_cross_attention:
                noise_sequence = upsample_block(
                    hidden_states=noise_sequence,
                    temb=emb,
                    res_hidden_states_tuple=res_samples,
                    encoder_hidden_states=reference_prompt,
                    upsample_size=upsample_size,
                    attention_mask=attention_mask,
                )
            else:
                noise_sequence = upsample_block(
                    hidden_states=noise_sequence, temb=emb, res_hidden_states_tuple=res_samples, upsample_size=upsample_size
                )
            print("noise sequence shape:", noise_sequence.shape)
        # 6. post-process
        noise_sequence = self.Unet_3D.conv_norm_out(noise_sequence)
        print("noise sequence shape:", noise_sequence.shape)
        noise_sequence = self.Unet_3D.conv_act(noise_sequence)
        print("noise sequence shape:", noise_sequence.shape)
        noise_sequence = self.Unet_3D.conv_out(noise_sequence)
        print("noise sequence shape:", noise_sequence.shape)

        # noise_outputs = self.Unet_3D(noise_sequence, timesteps, reference_prompt).sample # [B, 4 , F, H // 8, W // 8]
        # print(noise_outputs.shape)
        return noise_sequence
    
pretrained_model_path = "models/StableDiffusion/stable-diffusion-v1-5"
unet_additional_kwargs =  OmegaConf.create({'use_motion_module': True, 'motion_module_resolutions': [1, 2, 4, 8], 'unet_use_cross_frame_attention': False, 'unet_use_temporal_attention': False, 'motion_module_type': 'Vanilla', 'motion_module_kwargs': {'num_attention_heads': 8, 'num_transformer_block': 1, 'attention_block_types': ['Temporal_Self', 'Temporal_Self'], 'temporal_position_encoding': True, 'temporal_position_encoding_max_len': 24, 'temporal_attention_dim_div': 1, 'zero_initialize': True}})
vae = AutoencoderKL.from_pretrained(pretrained_model_path, subfolder="vae")
unet = UNet3DConditionModel.from_pretrained_2d(
    pretrained_model_path, subfolder="unet", 
    unet_additional_kwargs=OmegaConf.to_container(unet_additional_kwargs)
)
pretrained_reference_model_path = 'models/StableDiffusion/stable-diffusion-v1-5'
reference_unet = UNet2DConditionModel.from_pretrained(pretrained_reference_model_path, subfolder="unet")
clip = SentenceTransformer('clip-ViT-L-14').cuda()
pose_guider = PoseGuider3D()

animate_anyone_model = AnimateAnyoneModel(VAE = vae, CLIP = clip, ReferenceNet = reference_unet, Pose_Guider3D = pose_guider, Unet_3D = unet)
animate_anyone_model.cuda()
reference_image = torch.randn(1, 3, 256, 256).cuda()

url = "http://images.cocodataset.org/val2017/000000039769.jpg"
image = Image.open(requests.get(url, stream=True).raw).resize((256, 256))

pose_sequence = torch.randn(1, 16, 3, 256 ,256).cuda()

# noise_sequence = torch.randn(B, 4, F, H // 8, W // 8).cuda()
noise_sequence = torch.randn(1, 4, 16, 256 // 8, 256 // 8).cuda()
timesteps = torch.tensor(42).cuda()

output = animate_anyone_model(noise_sequence, reference_image, image, pose_sequence, timesteps)