import copy
import gc
import time
from datetime import datetime
import os
import torch
import numpy as np
import matplotlib.pyplot as plt

torch.set_num_threads(1)
from skimage.filters import threshold_otsu
from sklearn.metrics import f1_score
from torch.cuda.amp import autocast, GradScaler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from Code.Utils.loss import DiceLoss
from Code.Utils.antsImpl import getWarp_antspy, applyTransformation

scaler = GradScaler()


# os.environ["CUDA_VISIBLE_DEVICES"] = "4"

def saveImage(mri, mri_op, mri_lbl, ct, ct_op, op):
    # create grid of images
    figure = plt.figure(figsize=(10, 10))

    plt.subplot(231, title="MRI")
    plt.grid(False)
    plt.imshow(mri.permute(1, 2, 0), cmap="gray")

    plt.subplot(232, title="MRI OP")
    plt.grid(False)
    plt.imshow(mri_op.permute(1, 2, 0).to(torch.float), cmap="gray")

    plt.subplot(233, title="MRI LBL")
    plt.grid(False)
    plt.imshow(mri_lbl.permute(1, 2, 0).to(torch.float), cmap="gray")

    plt.subplot(234, title="CT")
    plt.grid(False)
    plt.imshow(ct.permute(1, 2, 0), cmap="gray")

    plt.subplot(235, title="CT OP")
    plt.grid(False)
    plt.imshow(ct_op.permute(1, 2, 0).to(torch.float), cmap="gray")

    plt.subplot(236, title="CT LBL")
    plt.grid(False)
    plt.imshow(torch.tensor(op).permute(1, 2, 0), cmap="gray")

    return figure


def train(dataloaders, modelPath, modelPath_bestweight, num_epochs, modelM1, modelM2, optimizer,
          log=False, device="cuda:0", model_Path_trained=""):
    if log:
        start_time = datetime.now().strftime("%Y.%m.%d.%H.%M.%S")
        TBLOGDIR = "runs/Training/Student_Unet3D/{}".format(start_time)
        writer = SummaryWriter(TBLOGDIR)
    modelM0 = torch.load(model_Path_trained)
    modelM0.eval()
    # model_tchr.to(device)
    best_model_wts = ""
    best_acc = 0.0
    best_val_loss = 99999
    since = time.time()
    # model.to(device)
    criterion = DiceLoss()
    store_idx = int(len(dataloaders[0]) / 2)
    for epoch in range(num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs))
        print('-' * 10)
        # Each epoch has a training and validation phase
        for phase in [0, 1]:
            if phase == 0:
                print("Model In Training mode")
                modelM1.train()  # Set model to training mode
                modelM2.train()  # Set model to training mode
            else:
                print("Model In Validation mode")
                modelM1.eval()  # Set model to evaluate mode
                modelM2.eval()  # Set model to evaluate mode

            running_loss = 0.0
            running_corrects = 0
            # Iterate over data.
            idx = 0
            for batch in tqdm(dataloaders[phase]):
                gc.collect()
                torch.cuda.empty_cache()
                # Get Data
                mri_batch, _, labels_batch, ct_actual = batch
                optimizer.zero_grad()

                # Section 1
                """
                    Input to model M1     : MRI
                    Input to model M1     : CT images
                    Output from model M1  : Warp Field
                """
                with torch.set_grad_enabled(phase == 0):
                    input = torch.cat((mri_batch, ct_actual), 0)
                    with autocast(enabled=True):
                        output_warp = modelM1(input.unsqueeze(1))[0]
                # Section 2
                """
                    Use warp field on GT MRI -> Pseudo GT
                    Use warp field on CT     -> Pseudo CTMR
                    
                    Input to model M2    : CT
                    Input to model M2    : MR or Pseudo CTMR
                    Output from model M2 : Image for training 
                    
                    Input to model M0 : Image for training  from Model M2
                    Input to model M0 : Pseudo GT
                    
                """
                waped_CT = output_warp * ct_actual  # Need to use interpolate to perform the transformation
                wapde_GT = output_warp * labels_batch  # Need to use interpolate to perform the transformation
                with torch.set_grad_enabled(phase == 0):
                    input = torch.cat((waped_CT, ct_actual), 0)
                    with autocast(enabled=True):
                        output_warp = modelM1(input.unsqueeze(1))[0]

                with torch.no_grad():
                    output_mri = modelM0(output_warp.unsqueeze(1))[0].squeeze().detach().cpu()
                    torch.cuda.empty_cache()

                # forward
                with torch.set_grad_enabled(phase == 0):
                    with autocast(enabled=True):
                        output_ct = modelM2(ct_actual.unsqueeze(1))[0]
                    pseudo_lbl = wapde_GT

                    loss, acc = criterion(output_ct[0].squeeze(0).squeeze(0),
                                          torch.from_numpy(pseudo_lbl.numpy()).to("cuda:3"))

                    # backward + optimize only if in training phase
                    if phase == 0:
                        scaler.scale(loss).backward()
                        scaler.step(optimizer)
                        scaler.update()

                        if epoch % 5 == 0:
                            print("Storing images", idx, epoch)
                            mri = mri_batch.squeeze()[8:9, :, :]
                            ct = ct_actual.squeeze()[8:9, :, :]
                            mri_op = output_mri[8:9, :, :].float()
                            ct_op = output_ct[0].squeeze()[8:9, :, :].detach().cpu().float()
                            mri_lbl = labels_batch.squeeze()[8:9, :, :].detach().cpu()
                            op = pseudo_lbl[8:9, :, :]
                            mri_op = (mri_op - mri_op.min()) / (mri_op.max() - mri_op.min())
                            ct_op = (ct_op - ct_op.min()) / (ct_op.max() - ct_op.min())
                            fig = saveImage(mri, mri_op, mri_lbl, ct, ct_op, op)
                            text = "Images on - " + str(epoch)
                            writer.add_figure(text, fig, epoch)
                    del output_mri
                    del output_ct
                    torch.cuda.empty_cache()
                    # statistics
                    running_loss += loss.item()
                    running_corrects += acc.item()
                    idx += 1

            epoch_loss = running_loss / len(dataloaders[phase])
            epoch_acc = running_corrects / len(dataloaders[phase])
            if phase == 0:
                mode = "Train"
                if log:
                    writer.add_scalar("Loss/Train", epoch_loss, epoch)
                    writer.add_scalar("Acc/Train", epoch_acc, epoch)
            else:
                mode = "Val"
                if log:
                    writer.add_scalar("Loss/Validation", epoch_loss, epoch)
                    writer.add_scalar("Acc/Validation", epoch_acc, epoch)

            # print('{} Loss: {:.4f} Acc: {:.4f}'.format(mode, epoch_loss, epoch_acc))
            print('{} Loss: {:.4f}'.format(mode, epoch_loss))

            # deep copy the model
            if phase == 1 and (epoch_acc > best_acc or epoch_loss < best_val_loss):
                print("Saving the best model weights")
                best_val_loss = epoch_loss
                best_acc = epoch_acc
                best_model_wts = copy.deepcopy(modelM1.state_dict())

        if epoch % 10 == 0:
            print("Saving the model")
            # save the model
            torch.save(modelM1, modelPath)
            # load best model weights
            if not best_model_wts == "":
                modelM1.load_state_dict(best_model_wts)
                torch.save(modelM1, modelPath_bestweight)

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))

    print("Saving the model")
    # save the model
    torch.save(modelM1, modelPath)
    # load best model weights
    modelM1.load_state_dict(best_model_wts)
    torch.save(modelM1, modelPath_bestweight)
