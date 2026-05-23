import torch
import torch.nn as nn
from torch import Tensor
from torchvision.models import resnet18, ResNet18_Weights


class CNNEncoderNet(nn.Module):

    def __init__(self, input_dim: int, output_dim: int = 1):
        super().__init__()

        self.name = 'cnnencodernet'
        self.cnn_encoder = ResNet18Encoder(in_channels=input_dim)
        self.temporal_encoder = TemporalEncoder(input_size=512, output_size=512)
        mlp4 = (512, 64, 32) + (output_dim,)
        self.decoder = get_decoder(mlp4)

    def forward(self, x: Tensor, mask: Tensor) -> Tensor:
        """
         Args:
            x(Tensor): B x T x C x H x W
            mask(Tensor): B x P
        """
        B, T, C, H, W = x.shape
        x = x.view(B * T, C, H, W)
        out = self.cnn_encoder(x)
        out = out.view(B, T, -1)
        if out.size(1) > 1:
            out = self.temporal_encoder(out)
        out = out[:, 0]
        out = self.decoder(out)
        return out


class ResNet18Encoder(nn.Module):

    def __init__(self, in_channels: int = 3):
        super().__init__()
        self.encoder = nn.Sequential(*list(resnet18(weights=ResNet18_Weights.DEFAULT).children())[:-1])

        if in_channels != 3:
            self._replace_first_conv(in_channels)

    def _replace_first_conv(self, in_channels: int) -> None:
        old = self.encoder[0]
        new_conv = nn.Conv2d(in_channels, old.out_channels, old.kernel_size, old.stride, old.padding, bias=old.bias is not None)
        nn.init.kaiming_uniform_(new_conv.weight.data, mode='fan_in', nonlinearity='relu')
        new_conv.weight.data[:, :min(in_channels, old.in_channels)] = old.weight.data[:, :min(in_channels, old.in_channels)]
        self.encoder[0] = new_conv

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.encoder(x).squeeze(dim=(2, 3))


class TemporalEncoder(nn.Module):
    def __init__(self, input_size=128, output_size=128, num_layers=1):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=output_size,
            num_layers=num_layers,
            batch_first=True
        )


    def forward(self, x):
        # x: (B, T, C)
        _, (h_n, _) = self.lstm(x)
        out = h_n[-1].unsqueeze(1)
        return out


def get_decoder(n_neurons):
    layers = []
    for i in range(len(n_neurons)-1):
        layers.append(nn.Linear(n_neurons[i], n_neurons[i+1]))
        if i < (len(n_neurons) - 2):
            layers.extend([
                nn.BatchNorm1d(n_neurons[i + 1]),
                nn.ReLU()
            ])
    m = nn.Sequential(*layers)
    return m