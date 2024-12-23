# Copyright 2024 LY Corporation

# LY Corporation licenses this file to you under the Apache License,
# version 2.0 (the "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at:

#   https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import math
from math import sqrt

import torch
import torch.nn as nn
import torch.nn.functional as F


class Mish(nn.Module):
    def forward(self, x):
        return x * torch.tanh(F.softplus(x))


class SinusoidalPosEmb(nn.Module):
    def __init__(self, dim, scale=1):
        super().__init__()
        self.dim = dim
        self.scale = scale

    def forward(self, x):
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device) * -emb)
        emb = self.scale * x[:, None] * emb[None, :]
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb


def Conv1d(*args, **kwargs):
    layer = nn.Conv1d(*args, **kwargs)
    nn.init.kaiming_normal_(layer.weight)
    return layer


@torch.jit.script
def silu(x):
    return x * torch.sigmoid(x)


class ResidualBlock(nn.Module):
    def __init__(self, encoder_hidden, residual_channels, kernel_size, dilation):
        super().__init__()
        self.dilated_conv = Conv1d(
            residual_channels,
            2 * residual_channels,
            kernel_size,
            padding=(kernel_size * dilation - dilation) // 2,
            dilation=dilation,
        )
        self.diffusion_projection = nn.Linear(residual_channels, residual_channels)
        self.conditioner_projection = Conv1d(encoder_hidden, 2 * residual_channels, 1)
        self.output_projection = Conv1d(residual_channels, 2 * residual_channels, 1)

    def forward(self, x, conditioner, diffusion_step, mask=None):
        diffusion_step = self.diffusion_projection(diffusion_step).unsqueeze(-1)
        conditioner = self.conditioner_projection(conditioner)
        y = x + diffusion_step

        y = self.dilated_conv(y) + conditioner

        gate, filter = torch.chunk(y, 2, dim=1)
        y = torch.sigmoid(gate) * torch.tanh(filter)

        y = self.output_projection(y)
        if mask is not None:
            y = y * mask
        residual, skip = torch.chunk(y, 2, dim=1)
        return (x + residual) / sqrt(2.0), skip


class DiffNet(nn.Module):
    def __init__(
        self,
        in_dim=80,
        encoder_hidden_dim=256,
        residual_layers=20,
        residual_channels=256,
        kernel_size=3,
        dilation_cycle_length=4,
        scale=1,
    ):
        super().__init__()
        self.in_dim = in_dim

        self.input_projection = Conv1d(in_dim, residual_channels, 1)
        self.diffusion_embedding = SinusoidalPosEmb(residual_channels, scale=scale)
        dim = residual_channels
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim * 4), Mish(), nn.Linear(dim * 4, dim)
        )
        self.residual_layers = nn.ModuleList(
            [
                ResidualBlock(
                    encoder_hidden_dim,
                    residual_channels,
                    kernel_size,
                    2 ** (i % dilation_cycle_length),
                )
                for i in range(residual_layers)
            ]
        )
        self.skip_projection = Conv1d(residual_channels, residual_channels, 1)
        self.output_projection = Conv1d(residual_channels, in_dim, 1)
        nn.init.zeros_(self.output_projection.weight)

    def forward(self, x, diffusion_step, cond, mask=None):
        """

        :param x: [B, M, T]
        :param diffusion_step: [B, 1]
        :param cond: [B, M, T]
        :return:
        """
        x = self.input_projection(x)  # x [B, residual_channel, T]

        x = F.relu(x)
        diffusion_step = self.diffusion_embedding(diffusion_step)
        diffusion_step = self.mlp(diffusion_step)
        skip = []
        for _, layer in enumerate(self.residual_layers):
            x, skip_connection = layer(x, cond, diffusion_step, mask)
            skip.append(skip_connection)

        x = torch.sum(torch.stack(skip), dim=0) / sqrt(len(self.residual_layers))
        x = self.skip_projection(x)
        x = F.relu(x)
        x = self.output_projection(x)  # [B, 80, T]
        return x
