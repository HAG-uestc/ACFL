import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import math

import sys
sys.path.append("./model/Temporal_shift/")

from cuda.shift import Shift


def import_class(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod

def conv_init(conv):
    nn.init.kaiming_normal_(conv.weight, mode='fan_out')
    nn.init.constant_(conv.bias, 0)


def bn_init(bn, scale):
    nn.init.constant_(bn.weight, scale)
    nn.init.constant_(bn.bias, 0)


class tcn(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=9, stride=1):
        super(tcn, self).__init__()
        pad = int((kernel_size - 1) / 2)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=(kernel_size, 1), padding=(pad, 0),
                              stride=(stride, 1))

        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU()
        conv_init(self.conv)
        bn_init(self.bn, 1)

    def forward(self, x):
        x = self.bn(self.conv(x))
        return x


class Shift_tcn(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=9, stride=1):
        super(Shift_tcn, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels

        self.bn = nn.BatchNorm2d(in_channels)
        self.bn2 = nn.BatchNorm2d(in_channels)
        bn_init(self.bn2, 1)
        self.relu = nn.ReLU(inplace=True)
        self.shift_in = Shift(channel=in_channels, stride=1, init_scale=1)
        self.shift_out = Shift(channel=out_channels, stride=stride, init_scale=1)

        self.temporal_linear = nn.Conv2d(in_channels, out_channels, 1)
        nn.init.kaiming_normal_(self.temporal_linear.weight, mode='fan_out')

    def forward(self, x):
        x = self.bn(x).contiguous()
        # shift1
        x = self.shift_in(x)
        x = self.temporal_linear(x)
        x = self.relu(x)
        # shift2
        x = self.shift_out(x)
        x = self.bn2(x)
        return x


class Shift_gcn(nn.Module):
    def __init__(self, in_channels, out_channels, A, coff_embedding=4, num_subset=3,num_point=25):
        super(Shift_gcn, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        if in_channels != out_channels:
            self.down = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1),
                nn.BatchNorm2d(out_channels)
            )
        else:
            self.down = lambda x: x
        
        self.Linear_weight = nn.Parameter(torch.zeros(in_channels, out_channels, requires_grad=True, device='cuda'), requires_grad=True)
        nn.init.normal_(self.Linear_weight, 0,math.sqrt(1.0/out_channels))

        self.Linear_bias = nn.Parameter(torch.zeros(1,1,out_channels,requires_grad=True,device='cuda'),requires_grad=True)
        nn.init.constant_(self.Linear_bias, 0)

        self.Feature_Mask = nn.Parameter(torch.ones(1,num_point,in_channels, requires_grad=True,device='cuda'),requires_grad=True)
        nn.init.constant_(self.Feature_Mask, 0)

        self.bn = nn.BatchNorm1d(num_point*out_channels)
        self.relu = nn.ReLU()

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)

        index_array = np.empty(num_point*in_channels).astype(np.int)
        for i in range(num_point):
            for j in range(in_channels):
                index_array[i*in_channels + j] = (i*in_channels + j + j*in_channels)%(in_channels*num_point)
        self.shift_in = nn.Parameter(torch.from_numpy(index_array),requires_grad=False)

        index_array = np.empty(num_point*out_channels).astype(np.int)
        for i in range(num_point):
            for j in range(out_channels):
                index_array[i*out_channels + j] = (i*out_channels + j - j*out_channels)%(out_channels*num_point)
        self.shift_out = nn.Parameter(torch.from_numpy(index_array),requires_grad=False)
        

    def forward(self, x0):
        n, c, t, v = x0.size()
        x = x0.permute(0,2,3,1).contiguous()

        # shift1
        x = x.view(n*t,v*c)
        x = torch.index_select(x, 1, self.shift_in)
        x = x.view(n*t,v,c)
        x = x * (torch.tanh(self.Feature_Mask)+1)

        x = torch.einsum('nwc,cd->nwd', (x, self.Linear_weight)).contiguous() # nt,v,c
        x = x + self.Linear_bias

        # shift2
        x = x.view(n*t,-1) 
        x = torch.index_select(x, 1, self.shift_out)
        x = self.bn(x)
        x = x.view(n,t,v,self.out_channels).permute(0,3,1,2) # n,c,t,v

        x = x + self.down(x0)
        x = self.relu(x)
        return x


class TCN_GCN_unit(nn.Module):
    def __init__(self, in_channels, out_channels, A, num_point, stride=1, residual=True):
        super(TCN_GCN_unit, self).__init__()
        self.gcn1 = Shift_gcn(in_channels, out_channels, A, num_point=num_point)
        self.tcn1 = Shift_tcn(out_channels, out_channels, stride=stride)
        self.relu = nn.ReLU()

        if not residual:
            self.residual = lambda x: 0

        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x
        else:
            self.residual = tcn(in_channels, out_channels, kernel_size=1, stride=stride)

    def forward(self, x):
        x = self.tcn1(self.gcn1(x)) + self.residual(x)
        return self.relu(x)


class Model(nn.Module):
    def __init__(self, num_class=60, num_point=25, num_person=2, graph=None, graph_args=dict(), in_channels=3, base_channel=64):
        super(Model, self).__init__()

        if graph is None:
            raise ValueError()
        else:
            Graph = import_class(graph)
            self.graph = Graph(**graph_args)

        A = self.graph.A
        self.data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)

        self.l1 = TCN_GCN_unit(in_channels, 64, A, num_point, residual=False)
        self.l2 = TCN_GCN_unit(64, 64, A, num_point)
        self.l3 = TCN_GCN_unit(64, 64, A, num_point)
        self.l4 = TCN_GCN_unit(64, 64, A, num_point)
        self.l5 = TCN_GCN_unit(64, 128, A, num_point,stride=2)
        self.l6 = TCN_GCN_unit(128, 128, A, num_point)
        self.l7 = TCN_GCN_unit(128, 128, A, num_point)
        self.l8 = TCN_GCN_unit(128, 256, A, num_point, stride=2)
        self.l9 = TCN_GCN_unit(256, 256, A, num_point)
        self.l10 = TCN_GCN_unit(256, 256, A, num_point)

        self.fc = nn.Linear(256, num_class)
        nn.init.normal_(self.fc.weight, 0, math.sqrt(2. / num_class))
        bn_init(self.data_bn, 1)

    def forward(self, x):
        N, C, T, V, M = x.size()

        x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()

        x = self.l1(x)
        x = self.l2(x)
        x = self.l3(x)
        x = self.l4(x)
        x = self.l5(x)
        x = self.l6(x)
        x = self.l7(x)
        x = self.l8(x)
        x = self.l9(x)
        x = self.l10(x)

        # N*M,C,T,V
        _, c_new, t_new, v_new = x.size()
        x = x.view(N, M, c_new, -1)
        feat_maps = x.clone().view(N, M, c_new, t_new, v_new)
        x = x.mean(3).mean(1)
        logits = self.fc(x)

        _, predict_label = torch.max(logits.data, 1)
        cams = []
        for i in range(N):
            weight = self.fc.weight[predict_label[i]].clone().view(1, 1, c_new, 1, 1)
            cam = (feat_maps[i] * weight).sum(2)
            cam = cam.mean(1).squeeze()
            cam = (cam - torch.min(cam)) / (cam.max()-cam.min())
            cams.append(cam.unsqueeze(0))

        return {'logits':logits, 'feature':x, 'cams':torch.cat(cams, 0)}


class HybridModel(nn.Module):
    def __init__(self, num_class=60, num_point=25, num_person=2, graph=None, graph_args=dict(), in_channels=3, base_channel=64):
        super(HybridModel, self).__init__()

        if graph is None:
            raise ValueError()
        else:
            Graph = import_class(graph)
            self.graph = Graph(**graph_args)

        A = self.graph.A
        self.joint_data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)
        self.bone_data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)

        self.jl1 = TCN_GCN_unit(in_channels, 64, A, num_point, residual=False)
        self.bl1 = TCN_GCN_unit(in_channels, 64, A, num_point, residual=False)
        self.jbl1 = TCN_GCN_unit(in_channels*2, 64, A, num_point, residual=False)
        self.l2 = TCN_GCN_unit(64, 64, A, num_point)
        self.l3 = TCN_GCN_unit(64, 64, A, num_point)
        self.l4 = TCN_GCN_unit(64, 64, A, num_point)
        self.l5 = TCN_GCN_unit(64, 128, A, num_point,stride=2)
        self.l6 = TCN_GCN_unit(128, 128, A, num_point)
        self.l7 = TCN_GCN_unit(128, 128, A, num_point)
        self.l8 = TCN_GCN_unit(128, 256, A, num_point, stride=2)
        self.l9 = TCN_GCN_unit(256, 256, A, num_point)
        self.l10 = TCN_GCN_unit(256, 256, A, num_point)

        self.fc = nn.Linear(256, num_class)
        nn.init.normal_(self.fc.weight, 0, math.sqrt(2. / num_class))
        bn_init(self.joint_data_bn, 1)
        bn_init(self.bone_data_bn, 1)

    def forward(self, x_j=None, x_b=None, style='joint'):

        if style == 'hybrid':
            N, C, T, V, M = x_j.size()
            x_j = x_j.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
            x_j = self.joint_data_bn(x_j)
            x_j = x_j.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()
            
            x_b = x_b.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
            x_b = self.bone_data_bn(x_b)
            x_b = x_b.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()

            x = torch.cat([x_j, x_b], 1)
            x = self.jbl1(x)
            
        else:

            if style == 'joint':
                x = x_j
            if style == 'bone':
                x = x_b

            N, C, T, V, M = x.size()

            x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
            if style == 'joint':
                x = self.joint_data_bn(x)
                x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()
                x = self.jl1(x)
            elif style == 'bone':
                x = self.bone_data_bn(x)
                x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()
                x = self.bl1(x)
        
        x = self.l2(x)
        x = self.l3(x)
        x = self.l4(x)
        x = self.l5(x)
        x = self.l6(x)
        x = self.l7(x)
        x = self.l8(x)
        x = self.l9(x)
        x = self.l10(x)

        # N*M,C,T,V
        c_new = x.size(1)
        x = x.view(N, M, c_new, -1)
        x = x.mean(3).mean(1)
        logits = self.fc(x)

        return {'logits':logits, 'feature':x}


class NaiveMultiModel(nn.Module):
    def __init__(self, num_class=60, num_point=25, num_person=2, graph=None, graph_args=dict(), in_channels=3,base_channel=64):
        super(NaiveMultiModel, self).__init__()

        if graph is None:
            raise ValueError()
        else:
            Graph = import_class(graph)
            self.graph = Graph(**graph_args)
        in_channels*=2
        A = self.graph.A
        self.data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)

        self.l1 = TCN_GCN_unit(in_channels, 64, A, num_point, residual=False)
        self.l2 = TCN_GCN_unit(64, 64, A, num_point)
        self.l3 = TCN_GCN_unit(64, 64, A, num_point)
        self.l4 = TCN_GCN_unit(64, 64, A, num_point)
        self.l5 = TCN_GCN_unit(64, 128, A, num_point,stride=2)
        self.l6 = TCN_GCN_unit(128, 128, A, num_point)
        self.l7 = TCN_GCN_unit(128, 128, A, num_point)
        self.l8 = TCN_GCN_unit(128, 256, A, num_point, stride=2)
        self.l9 = TCN_GCN_unit(256, 256, A, num_point)
        self.l10 = TCN_GCN_unit(256, 256, A, num_point)

        self.fc = nn.Linear(256, num_class)
        nn.init.normal_(self.fc.weight, 0, math.sqrt(2. / num_class))
        bn_init(self.data_bn, 1)

    def forward(self, x):
        N, C, T, V, M = x.size()

        x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V).contiguous()

        x = self.l1(x)
        x = self.l2(x)
        x = self.l3(x)
        x = self.l4(x)
        x = self.l5(x)
        x = self.l6(x)
        x = self.l7(x)
        x = self.l8(x)
        x = self.l9(x)
        x = self.l10(x)

        # N*M,C,T,V
        c_new = x.size(1)
        x = x.view(N, M, c_new, -1)
        x = x.mean(3).mean(1)
        logits = self.fc(x)

        return {'logits':logits, 'feature':x}

