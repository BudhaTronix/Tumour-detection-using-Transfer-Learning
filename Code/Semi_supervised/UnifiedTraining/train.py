import os
import sys
import time
import random
import numpy
from datetime import datetime

import matplotlib.pyplot as plt
import torch
import torch.nn.functional as F

torch.set_num_threads(1)
from torch.cuda.amp import autocast, GradScaler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.getcwd())))
print(ROOT_DIR)
sys.path.insert(1, ROOT_DIR + "/")
from Code.Utils.loss import DiceLoss
from Code.Semi_supervised.mscgunet.train import Mscgunet

scaler = GradScaler()

torch.cuda.manual_seed(42)
torch.manual_seed(42)
numpy.random.seed(seed=42)
random.seed(42)


# Change image plot - mri with label
# Give meaningful names to variables
def saveImage(mri, mri_lbl, ct, ctmri_merge, ct_op, pseudo_gt):
    # create grid of images
    figure = plt.figure(figsize=(10, 10))

    plt.subplot(231, title="Inp : MRI")
    plt.grid(False)
    plt.imshow(mri.permute(1, 2, 0), cmap="gray")

    plt.subplot(232, title="Inp : CT")
    plt.grid(False)
    plt.imshow(ct.permute(1, 2, 0), cmap="gray")

    plt.subplot(233, title="CT-MR")
    plt.grid(False)
    plt.imshow(ctmri_merge.permute(1, 2, 0).to(torch.float), cmap="gray")

    plt.subplot(234, title="MRI LBL")
    plt.grid(False)
    plt.imshow(mri_lbl.permute(1, 2, 0).to(torch.float), cmap="gray")

    plt.subplot(235, title="Pseudo CT LBL")
    plt.grid(False)
    plt.imshow(pseudo_gt.permute(1, 2, 0), cmap="gray")

    plt.subplot(236, title="Output")
    plt.grid(False)
    plt.imshow(ct_op.permute(1, 2, 0).to(torch.float), cmap="gray")

    return figure


def train(dataloaders, M0_model_path, M0_bw_path, M1_model_path, M1_bw_path, num_epochs, modelM0, modelM1, optimizer, log=False, logPath=""):
    if log:
        start_time = datetime.now().strftime("%Y.%m.%d.%H.%M.%S")
        TBLOGDIR = logPath + "{}".format(start_time)
        writer = SummaryWriter(TBLOGDIR)

    GPU_ID_M0 = "cuda:" + str(next(modelM0.parameters()).device.index)

    best_model_wts = ""
    best_acc = 0.0
    best_val_loss_0 = 99999
    best_val_loss_1 = 99999
    since = time.time()
    criterion = DiceLoss()
    print("Before Training")
    for epoch in range(num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs))
        print('-' * 10)
        # Each epoch has a training and validation phase
        for phase in [0, 1]:
            if phase == 0:
                print("Model In Training mode")
                modelM0.train()  # Set model to evaluate mode

            else:
                print("Model In Validation mode")
                modelM0.eval()  # Set model to evaluate mode

            running_loss_0 = 0.0
            running_loss_1 = 0.0
            running_corrects = 0
            # Iterate over data.
            idx = 0
            for batch in tqdm(dataloaders[phase]):

                # Get Data
                mri_batch, labels_batch, ct_batch = batch

                optimizer.zero_grad()

                with torch.set_grad_enabled(phase == 0):
                    with autocast(enabled=False):
                        loss_1, fully_warped_image_yx, pseudo_lbl = modelM1.lossCal(ct_batch, mri_batch, labels_batch)

                        output_ct = modelM0(fully_warped_image_yx.to(GPU_ID_M0)).squeeze()
                        loss_0, acc = criterion(output_ct, pseudo_lbl.squeeze().to(GPU_ID_M0))

                        total_loss = loss_0 + loss_1

                    if phase == 0:
                        if autocast:
                            scaler.scale(total_loss).backward()
                            scaler.step(optimizer)
                            scaler.update()
                        else:
                            total_loss.backward()
                            optimizer.step()

                        if epoch % 1 == 0 and log and idx == 0:
                            temp = labels_batch.squeeze().detach().cpu()
                            slice = 0
                            for i in range(len(temp)):
                                if temp[i].max() == 1:
                                    slice = i
                                    break
                            mri = mri_batch.squeeze()[slice, :, :].unsqueeze(0)
                            ct = ct_batch.squeeze()[slice, :, :].unsqueeze(0)
                            ctmri_merge = fully_warped_image_yx.squeeze()[slice, :, :].unsqueeze(0).float().clone().detach().cpu()
                            ct_op = output_ct[slice, :, :].unsqueeze(0).clone().detach().cpu().float()
                            mri_lbl = labels_batch.squeeze()[slice, :, :].unsqueeze(0).clone().detach().cpu()
                            pseudo_gt = pseudo_lbl.squeeze()[slice, :, :].unsqueeze(0).clone().detach().cpu()

                            ctmri_merge = (ctmri_merge - ctmri_merge.min()) / (ctmri_merge.max() - ctmri_merge.min())
                            ct_op = (ct_op - ct_op.min()) / (ct_op.max() - ct_op.min())

                            fig = saveImage(mri, mri_lbl, ct, ctmri_merge, ct_op, pseudo_gt)
                            text = "Images on - " + str(epoch)
                            writer.add_figure(text, fig, epoch)

                    # statistics
                    running_loss_0 += loss_0.item()
                    running_loss_1 += loss_1.item()

                    running_corrects += acc.item()
                    idx += 1

            epoch_loss_0 = running_loss_0 / len(dataloaders[phase])
            epoch_loss_1 = running_loss_1 / len(dataloaders[phase])
            # epoch_acc = running_corrects / len(dataloaders[phase])
            if phase == 0:
                mode = "Train"
                if log:
                    writer.add_scalar("Train/Loss_0", epoch_loss_0, epoch)
                    writer.add_scalar("Train/Loss_1", epoch_loss_1, epoch)
            else:
                mode = "Val"
                if log:
                    writer.add_scalar("Validation/Loss_0", epoch_loss_0, epoch)
                    writer.add_scalar("Validation/Loss_1", epoch_loss_1, epoch)

            print('{} Loss_0: {:.4f} Loss_1: {:.4f}'.format(mode, epoch_loss_0, epoch_loss_1))

            # deep copy the model
            if phase == 1:
                if epoch_loss_0 < best_val_loss_0:
                    print("Saving the best model weights of Model 0")
                    best_val_loss_0 = epoch_loss_0
                    torch.save(modelM0.state_dict(), M0_bw_path)
                if epoch_loss_1 < best_val_loss_1:
                    print("Saving the best model weights of Model 1")
                    best_val_loss_1 = epoch_loss_1
                    # torch.save(modelM1.state_dict(), M1_bw_path)

        if epoch % 10 == 0:
            print("Saving the model")
            # save the model
            torch.save(modelM0.state_dict(), M0_model_path)
            #mtorch.save(modelM1.state_dict(), M1_model_path)

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))

    # save the model
    print("Saving the model before exiting")
    torch.save(modelM0.state_dict(), M0_model_path)
    # torch.save(modelM1.state_dict(), M1_model_path)