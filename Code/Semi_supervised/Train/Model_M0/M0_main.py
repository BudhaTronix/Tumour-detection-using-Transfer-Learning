import sys
import logging
import torch
import torch.optim as optim
import torchio as tio

from Code.Semi_supervised.Train.Model_M0.M0_dataloader import TeacherCustomDataset
from Code.Semi_supervised.Train.Model_M0.M0_train import train
from Model.M0 import U_Net_M0
from Model.DeepSupAttUNet3D import DeepSupAttentionUnet

torch.set_num_threads(1)

try:
    from Code.Utils.CSVGenerator import checkCSV
except ImportError:
    sys.path.insert(1, '/project/mukhopad/tmp/LiverTumorSeg/Code/Utils/')
    from CSVGenerator import checkCSV


class M0_Pipeline:
    def __init__(self, dataset_path, M0_model_path, M0_bw_path, loss_fn, model_type, isChaos=False, device="cuda",
                 log_path="runs/Training/", epochs=100, seed_val=42):
        self.batch_size = 1

        # Model Weights
        self.M0_model_path = M0_model_path
        self.M0_bw_path = M0_bw_path
        self.loss_fn = loss_fn
        self.model_type = model_type

        self.dataset_path = dataset_path
        self.logPath = log_path + "_Model_M0/"
        self.csv_file = "dataset_teacher.csv"
        self.transform_val = (32, 256, 256)
        self.num_epochs = epochs
        self.device = device
        self.seed = seed_val
        self.isChaos = isChaos

    def defineModel(self):
        if self.model_type == "DeepSup":
            model = DeepSupAttentionUnet(1, 1)
        else:
            model = U_Net_M0()
        return model

    @staticmethod
    def defineOptimizer(model):
        optimizer = optim.Adam(model.parameters(), lr=0.001)
        return optimizer

    def displayDetails(self):
        logging.info("############################# START M0 Model Training #############################")
        logging.info("Starting with Model M0 Training")
        logging.info("Logging Path     : " + self.logPath)
        logging.info("Model M0 Path    : " + self.M0_model_path)
        logging.info("Model M0 BW Path : " + self.M0_bw_path)
        logging.info("Device           : " + str(self.device))
        logging.info("Epochs total     : " + str(self.num_epochs))
        logging.info("Loss Function    : " + self.loss_fn)
        logging.info("Model Type       : " + self.model_type)

    def trainModel(self):
        self.displayDetails()

        model = self.defineModel()
        optimizer = self.defineOptimizer(model)

        transform = tio.CropOrPad(self.transform_val)

        checkCSV(dataset_Path=self.dataset_path, csv_FileName=self.csv_file, overwrite=True)
        dataset = TeacherCustomDataset(self.isChaos, self.dataset_path, self.csv_file, transform)

        train_size = int(0.8 * len(dataset))
        val_size = len(dataset) - train_size

        train_dataset, val_dataset = torch.utils.data.random_split(dataset,
                                                                   [train_size, val_size],
                                                                   generator=torch.Generator().manual_seed(self.seed))

        logging.info("Train Indices  : {}".format(str(train_dataset.indices)))
        logging.info("Val   Indices  : {}".format(str(val_dataset.indices)))

        # Training and Validation Section
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True,
                                                   generator=torch.Generator().manual_seed(self.seed))
        validation_loader = torch.utils.data.DataLoader(val_dataset, batch_size=self.batch_size, shuffle=True,
                                                        generator=torch.Generator().manual_seed(self.seed))

        dataloaders = [train_loader, validation_loader]
        train(dataloaders, self.M0_model_path, self.M0_bw_path, self.num_epochs, model, optimizer, self.device,
              self.loss_fn, self.model_type,
              log=logging, logPath=self.logPath)
