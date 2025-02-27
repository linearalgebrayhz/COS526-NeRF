import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from encoder import PositionalEncoder


# Model
class NeRF(nn.Module):
    def __init__(self, D=8, W=256, output_ch=4, skips=[4], embedder=PositionalEncoder(10,0), embedder_dirs=None, use_viewdirs=False, input_ch=3, input_ch_views=3):
        """_summary_

        Args:
            D (int, optional): Depth of the MLP. Defaults to 8.
            W (int, optional): Width of each MLP Layer. Defaults to 256.
            output_ch (int, optional): Output channels (RGB+density). Defaults to 4.
            skips (list, optional): Layers where skip connections are added. Defaults to [4].
            embedder (_type_, optional): Positional encoder for 3D coordinates. Defaults to PositionalEncoder(10,0).
            embedder_dirs (_type_, optional): Positional encoder for viewing directions. Defaults to None.
            use_viewdirs (bool, optional): Whether to use view directions. Defaults to False.
            input_ch (int, optional): Input dimension for 3D coordinates. Defaults to 3.
            input_ch_views (int, optional): Input dimension for viewing directions. Defaults to 3.
        """
        super(NeRF, self).__init__()
        # Adjusts input_ch based on the positional encoder's output dimension.
        if embedder is not None:
            input_ch=embedder.out_dim
        # Sets input_ch_views to 0 if view directions are not used.
        # embedder_dirs us a separate positional encoder for view directions
        # Eables view dependent effects like specular highlights
        if use_viewdirs:
            if embedder_dirs is None:
                input_ch_views = input_ch_views
            else:
                input_ch_views = embedder_dirs.out_dim
        else:
            input_ch_views = 0

        self.D = D
        self.W = W
        self.input_ch = input_ch # after positional encoding: 3 + 3*2*L
        self.input_ch_views = input_ch_views
        self.skips = skips
        self.use_viewdirs = use_viewdirs

        self.embedder = embedder
        self.embedder_dirs = embedder_dirs
        
        #------------------------------------------------------
        #----- PLEASE FILL IN COMPUTATIONS FOR 
        #----- self.pts_linears: Pytorch ModuleList. Holds the MLP architecture from Fig 7 before concatenating with viewing directions
        #----- self.feature_linear: Pytorch Module. The MLP layer that outputs a feature vector, that will later be concatenated with encoding of viewing directions
        #----- self.alpha_linear: Pytorch Module. The MLP layer that outputs volume density
        #----- self.rgb_linear: Pytorch Module. The MLP layer that emits RGB radiance
        #------------------------------------------------------
        
        # assert False, 'Please finish the code before removing this assertion'
        
        # first layer + subsequent D-1 layers
        # For layers in `skip`, add skip connection with original input. The dimension becomes W + input_ch
        self.pts_linears = nn.ModuleList(
            [nn.Linear(input_ch, W)] + 
            [nn.Linear(W, W) if i not in self.skips else nn.Linear(W + input_ch, W) for i in range(D-1)])

        ### Implementation according to the official code release (https://github.com/bmild/nerf/blob/master/run_nerf_helpers.py#L104-L105)
        # combines spatial and view-direction info
        # compre features to W//2
        self.views_linears = nn.ModuleList([nn.Linear(input_ch_views + W, W // 2)])

        ### Implementation according to the paper
        # self.views_linears = nn.ModuleList(
        #     [nn.Linear(input_ch_views + W, W//2)] + [nn.Linear(W//2, W//2) for i in range(D//2)])

        if use_viewdirs:
            self.feature_linear = nn.Linear(W, W)
            self.alpha_linear = nn.Linear(W, 1)
            self.rgb_linear = nn.Linear(W//2, 3)
        else:
            self.output_linear = nn.Linear(W, output_ch)

        # Architecture Flow for use_viewdirs = True
        """
        1. Spatial MLP (pts_linears):

            Input: Positionally encoded 3D coordinates (input_ch).
            Output: W-dimensional features + density (alpha).
            
        2. View-Dependent Branch:
        
            concatenate spatial features with encoded view directions
            view_linears: (W + input_ch_views) → W//2
            rgb_linear: W//2 → 3 (RGB)
        
        """
    def forward(self, inputs, viewdirs, netchunk=1024 * 64):
        """
        Forward pass of inputs and viewing directions through the nerf model
        Netchunk is used to avoid OOM
        """
        if netchunk is None:
            netchunk = inputs.shape[0]

        # Apply positional encoding to input points
        inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])
        embedded = self.embedder(inputs_flat)

        if viewdirs is not None:
            # Apply positional encoding to ray directions
            input_dirs = viewdirs[:, None].expand(inputs.shape)
            input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]])
            embedded_dirs = self.embedder_dirs(input_dirs_flat)
            embedded = torch.cat([embedded, embedded_dirs], -1)

        out = []
        # Netchunk is used here to avoid memory overflow
        for i in range(0, embedded.shape[0], netchunk):
            x = embedded[i:i + netchunk]

            # Predict color and densities for a chunk of samples
            input_pts, input_views = torch.split(x, [self.input_ch, self.input_ch_views], dim=-1)
            h = input_pts
            for i, l in enumerate(self.pts_linears):
                h = self.pts_linears[i](h)
                h = F.relu(h)
                if i in self.skips:
                    h = torch.cat([input_pts, h], -1)

            if self.use_viewdirs:
                alpha = self.alpha_linear(h)
                feature = self.feature_linear(h)
                h = torch.cat([feature, input_views], -1)

                for i, l in enumerate(self.views_linears):
                    h = self.views_linears[i](h)
                    h = F.relu(h)

                rgb = self.rgb_linear(h)
                outputs_chunk = torch.cat([rgb, alpha], -1)
            else:
                outputs_chunk = self.output_linear(h)

            out.append(outputs_chunk)
        # collect all chunks and reshape
        out_flat = torch.cat(out)

        outputs = torch.reshape(out_flat, list(inputs.shape[:-1]) + [out_flat.shape[-1]])
        return outputs

    # Loads pretrained weights from the original NeRF implementation (Keras) into PyTorch layers.
    def load_weights_from_keras(self, weights):
        assert self.use_viewdirs, "Not implemented if use_viewdirs=False"

        # Load pts_linears
        for i in range(self.D):
            idx_pts_linears = 2 * i
            self.pts_linears[i].weight.data = torch.from_numpy(np.transpose(weights[idx_pts_linears]))
            self.pts_linears[i].bias.data = torch.from_numpy(np.transpose(weights[idx_pts_linears + 1]))

        # Load feature_linear
        idx_feature_linear = 2 * self.D
        self.feature_linear.weight.data = torch.from_numpy(np.transpose(weights[idx_feature_linear]))
        self.feature_linear.bias.data = torch.from_numpy(np.transpose(weights[idx_feature_linear + 1]))

        # Load views_linears
        idx_views_linears = 2 * self.D + 2
        self.views_linears[0].weight.data = torch.from_numpy(np.transpose(weights[idx_views_linears]))
        self.views_linears[0].bias.data = torch.from_numpy(np.transpose(weights[idx_views_linears + 1]))

        # Load rgb_linear
        idx_rbg_linear = 2 * self.D + 4
        self.rgb_linear.weight.data = torch.from_numpy(np.transpose(weights[idx_rbg_linear]))
        self.rgb_linear.bias.data = torch.from_numpy(np.transpose(weights[idx_rbg_linear + 1]))

        # Load alpha_linear
        idx_alpha_linear = 2 * self.D + 6
        self.alpha_linear.weight.data = torch.from_numpy(np.transpose(weights[idx_alpha_linear]))
        self.alpha_linear.bias.data = torch.from_numpy(np.transpose(weights[idx_alpha_linear + 1]))