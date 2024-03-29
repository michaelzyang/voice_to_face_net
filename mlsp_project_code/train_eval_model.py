"""
Script for training and evaluating a given model architecture
v0.8 December 2, 2019

Script to train a voice to voice+face model for given hyperparameters and a 
given (trained) voice autoencoder.
Then runs an evaluation routine on the trained model and saves the results.

INPUTS:
- a trained voice autoencoder - edit AE_save_state to be the filepath of its save state 
- edit the hyperparameters listed before the main function definition
- note: provide the training voice data ONLY, not the voices for any IDs in the 
        validation or test sets, as we do not want the model to see any 
        validation or test set faces.
OUTPUTS:
- saves the full model model state at "./model_state.pth"
- saves the convergence plot and list of loss at each iteration in a folder named "./convergence"
"""

import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader, random_split
from glob import glob
from datetime import datetime
import matplotlib.pyplot as plt
import sys


# TODO this weekend
# - pick the model/hyperparams that yields best eval_metric
# - optional: train model using best hyperparams on train+validate data
# - get eval_metric of the best model using the test data

# Paths
voice_train_path = "data_mlsp/train_data/"
validation_voice_filepath = "data_mlsp/valid_data/"
face_path = "data_mlsp/facespecs/"
AE_save_state = "./AE_model_state.pth"

# Global training parameters
BATCH_SIZE = 5
voice_loss = nn.MSELoss()
LEARNING_RATE = 1e-3
CUDA_AVAIL = False

# Autoencoder training parameters
AE_NUM_EPOCHS = 30

# Full model training parameters
ALPHA = 0.5
ORTHOGONALIZE_B = False
NUM_EPOCHS = 100
face_loss = nn.MSELoss()
FACE_STD = 28 # std dev of pixel values from a subsample of 93 faces. used to scale faces to have std ~= 1

# Validation parameters
# validation_IDs =  # Included at the bottom of script
lineup_length = 10
top_n = 10

# # EITHER EDIT THIS CLASS OR IMPORT IT FROM YOUR OWN SCRIPT
class Voice_Autoencoder(nn.Module):
    def __init__(self):
        """
        w_length: the length of the bottleneck vector i.e. # of basis faces used
        face_length: the height * width of the face images
        """
        super(Voice_Autoencoder, self).__init__()
        
        self.w_length = None
        
        self.encoder = nn.ModuleList(
            [
                nn.Conv2d(1, 8, kernel_size=3, stride=1, padding=1), #1025 x 251
                nn.ReLU(True),
                #nn.MaxPool2d(2, stride=2), 
                nn.Conv2d(8, 16, kernel_size=5, stride=2, padding=1), #512 x 125
                nn.ReLU(True),
                #nn.MaxPool2d(2, stride=2),
                nn.Conv2d(16, 32, kernel_size=5, stride=2, padding=2), #256 x 63
                nn.ReLU(True),
                nn.MaxPool2d(2, stride=2),                             #128 x 31
                nn.Conv2d(32, 1, kernel_size=3, stride=1, padding=1)   #128 x 31
            ]
        )
        
        self.decoder = nn.ModuleList(
            [
                nn.ConvTranspose2d(1, 32, kernel_size=3, stride=1, padding=1), # 128 x 31
                nn.ReLU(True),
                nn.ConvTranspose2d(32, 16, kernel_size=3, stride=2, padding=(1,0), output_padding = (1,0)), # 256 x 63
                nn.ReLU(True),
                nn.ConvTranspose2d(16, 8, kernel_size=3, stride=2, padding=(1,1), output_padding = (1,0)), # 512 x 125
                nn.ReLU(True),
                nn.ConvTranspose2d(8, 1, kernel_size=3, stride=2, padding=0, output_padding = 0), # 1025 x 251
                nn.Tanh()
            ]
        )

    def forward(self, v):
        # start encoder
        for layer in self.encoder:
            v = layer.forward(v)
            #print(v.shape)
        
        # collapse final feature map into a vector by taking average across time
        N, _, H, _ = v.shape
        w = v.mean(dim=3)
        w = w.view(N, H)
        
        if self.w_length == None:
            self.w_length = H
        
        # start decoder
        for layer in self.decoder:
            v = layer.forward(v)
            #print(v.shape)
        
        return v, w







def main():
    if sys.argv[1] != "sequential" and sys.argv[1] != "combined_only":
        print("usage: pass \"sequential\" to train autoencoder and full model sequentially")
        print("pass \"combined_only\" to use a pre-trained autoencoder")
    else:
        # print cuda status
        CUDA_AVAIL = torch.cuda.is_available()
        if (CUDA_AVAIL):
            device = torch.cuda.current_device()
            print("cuda available on device: ", torch.cuda.get_device_name(device))
        else:
            print("cuda not available.")
        
        # import voice data
        print("Importing voice data. ", datetime.now())
        dataloader = prep_dataloader(cuda=CUDA_AVAIL)
        
        # train autoencoder if specified
        if sys.argv[1] == "sequential":
            main_ae(dataloader)

        # import face data
        print("Importing face data as vectors into a dictionary. ", datetime.now())
        face_dict = make_face_dict(path=face_path, face_std=FACE_STD)
        
        # train model and save outputs
        print("Loading Voice Autoencoder model. ", datetime.now())
        AE_model = Voice_Autoencoder() # requires correct class definition
        if CUDA_AVAIL:
            AE_model = AE_model.cuda()
        AE_optimizer = torch.optim.Adam(AE_model.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)
        load_state(AE_save_state, AE_model, AE_optimizer, print_model=True)

        print("Training model. ", datetime.now())
        model = full_model(AE_model, face_shape=(128,128))
        if CUDA_AVAIL:
            model = model.cuda()
        train_model(model, dataloader, face_dict)
        print("Model training complete. ", datetime.now())

        print("Start validating model. ", datetime.now())
        top_n_acc = evaluate_model(model, 
                                evaluate_IDs=validation_IDs, 
                                voice_eval_path=validation_voice_filepath, 
                                face_dict=face_dict, 
                                lineup_length=lineup_length,
                                top_n = top_n,
                                save=True)
        plot_top_n_acc(top_n_acc, save=True)
        print("Validation complete. ", datetime.now())



class full_model(nn.Module):
    def __init__(self, AE_model, face_shape=(128,128)):
        """
        AE_model: a voice autoencoder model whose forward function returns v, w,
                  where v is the voice reconstruction and w is the embedding
        face_length: the height * width of the face images
        """
        super(full_model, self).__init__()
        self.AE_model = AE_model
        self.w_length = AE_model.w_length
        self.face_length = face_shape[0] * face_shape[1]
        self.B = nn.Linear(self.w_length, self.face_length, bias=False)

    def forward(self, v):
        # run voice input through AE
        v, w = self.AE_model(v)
        
        # face construction
        f = self.B(w)
        
        return v, f

    def predict(self, ID, voice_filepath, face_std=FACE_STD, standardize=True):
        if type(ID) == torch.Tensor:
            ID = ID.item()
        # get all voice files that correspond to that ID
        format = "voice_{}*".format(ID)
        voice_filenames = get_filenames(voice_filepath, filename_format=format)
        assert(len(voice_filenames) > 0)

        # construct spectrograms tensor
        matrices = [] # the spectrograms
        for v_file in voice_filenames:
            # get spectrogram
            matrix = np.loadtxt(v_file, delimiter=',', dtype=np.float32)
            if standardize:
                matrix = (matrix - np.mean(matrix)) / np.std(matrix)
            matrices.append(matrix)
        voices_tensor = torch.Tensor(matrices)
        N, D, M = voices_tensor.shape
        voices_tensor = voices_tensor.view(N, 1, D, M) # insert channel dimension

        # get average of the face reconstructions as a 2D numpy array
        _, f = self.forward(voices_tensor) # reconstruct face from every voice
        f_mean = f.mean(dim=0) # get the average of all the reconstructed faces
        f_mean = f_mean.detach().numpy() # convert to numpy array
        f_mean = f_mean.reshape((128,128))
        f_mean = f_mean * face_std # scale back up
        return f_mean








# Main routine functions

def prep_dataloader(cuda=False):
    train_voice_filenames = get_filenames(voice_train_path)
    train_dataset = voice_face(train_voice_filenames, standardize=True)
    dataloader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
    if cuda:
        curr_device = torch.cuda.current_device()
        dataloader = DeviceDataloader(dataloader, curr_device)
    return dataloader


def make_face_dict(path=face_path, face_std=FACE_STD, IDs=None):
    """
    USAGE: 
    - face_dict[6] returns a 2D numpy array of the face with ID=6
    INPUTS:
    - IDs: a 1D tensor or iterable of int IDs
    - path: a filename format with {} in place of the ID in the filename.
    """
    face_dict = {}
    if IDs == None:
        f_files = get_filenames(path, filename_format="face_*")
        for f_file in f_files:
            ID = get_n(f_file)
            face_arr = np.loadtxt(f_file, delimiter=',').flatten()
            face_arr = face_arr / face_std # scale to have std dev ~= 1
            face_dict[ID] = face_arr
    else:
        if path[-1] != '/':
            path += '/'
        
        for ID in IDs:
            if type(ID) == torch.Tensor:
                ID = ID.item()
            assert(type(ID) == int)
            
            f_file = path + "face_{}.csv".format(ID)
            face_arr = np.loadtxt(f_file, delimiter=',').flatten()
            face_arr = face_arr / face_std # scale to have std dev ~= 1
            face_dict[ID] = face_arr
    return face_dict


def get_filenames(paths, filename_format="voice_*"):
    # in case only one path given, make it a list so that it's iterable
    if type(paths) == str:
        paths = [paths]

    # get lists of all voice filenames
    filenames = []
    for path in paths:
        if path[-1] != '/':
            path += '/'
        filenames += glob(path + filename_format)
   
    return filenames


class voice_face(Dataset):
    def __init__(self, voice_filenames, standardize=False):
        """
        Preconditions: csv files must contain matrices of the same dimension
        Args:
            voice_filenames (string or list): list of filenames/pathnames of csv files with spectrogram matrices
                                              assumes format voice_{n}_{m}.csv, 
                                              where n is the data ID and m is the spectrogram number for that speaker
            standardise (boolean):            whether to standardize the spectrograms
        """
        # ensure inputs are lists
        if type(voice_filenames) == str:
            voice_filenames = [voice_filenames]
        assert(type(voice_filenames) == list)
                
        # load voice spectrograms one by one
        face_IDs = [] # the face IDs associated with each spectrogram
        matrices = [] # the spectrograms
        for v_file in voice_filenames:
            # get n, the data ID 
            n, _ = get_n_m(v_file)
            face_IDs.append(n)
            
            # get spectrogram
            matrix = np.loadtxt(v_file, delimiter=',', dtype=np.float32)
            if standardize:
                matrix = (matrix - np.mean(matrix)) / np.std(matrix)
            matrices.append(matrix)
        
        # construct spectrograms tensor
        self.X = torch.Tensor(matrices)
        N, D, M = self.X.shape
        self.X = self.X.view(N, 1, D, M) # insert channel dimension
        
        # construct face_IDs tensor
        self.y = torch.tensor(face_IDs)
        
        assert(self.X.shape[0] == self.y.shape[0])
        
    def __len__(self):
        return self.y.shape[0]

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]


def get_n_m(v_file):
    """
    Takes a voice file name of the form path/voice_{n}_{m}.csv
    And outputs n and m as integers.
    """
    v_file = v_file.split('/')[-1] # strip the pathname if it exists
    v_file, _ = v_file.split('.') # strip the file extension
    _, n, m = v_file.split('_') # get n and m from the filename
    return int(n), int(m)


def get_n(f_file):
    """
    Takes a face file name of the form path/face_{n}.csv
    And outputs n as an integer.
    """
    f_file = f_file.split('/')[-1] # strip the pathname if it exists
    f_file, _ = f_file.split('.') # strip the file extension
    _, n = f_file.split('_') # get n from the filename
    return int(n)

def combined_loss(model_output, labels, face_dict):
    """
    REQUIRES
    - face dictionary
    - face_retrieve_loss function
    INPUTS
    - model_output: a tuple of (voice_output, face_output), both tensors
    - labels: a tuple of (voice_data, face_id), a tensor and a 1-D tensor
    """
    # unpack input
    voice_outputs, face_outputs = model_output
    voice_data, IDs = labels
    
    # compute combined loss
    combined_loss = face_retrieve_loss(face_outputs, IDs, face_dict) + ALPHA * voice_loss(voice_outputs, voice_data)
    return combined_loss


def face_retrieve_loss(face_outputs, IDs, face_dict):
    """
    Retrieves ground truth face tensor given the IDs, computes and returns loss
    REQUIRES
    - face dictionary
    - face_loss function
    INPUTS
    - face_outputs: tensor of face matrices
    - IDs: tensor of IDs
    """
    # construct true faces tensor
    true_faces = []
    for ID in IDs:
        ID_int = ID.item()
        true_faces.append(face_dict[ID_int])
    true_faces = torch.Tensor(true_faces)

    # compute and return loss
    loss = face_loss(face_outputs, true_faces)
    return loss

def train_model(model, dataloader, face_dict):
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)
    
    loss_epochs = []
    for epoch in range(NUM_EPOCHS):
        for batch in dataloader:
            # ===================forward=====================
            voice_data, IDs = batch
            # voice_outputs, face_outputs = model(voice_data)
            loss = combined_loss(model(voice_data), batch, face_dict)
            # ===================backward====================
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if ORTHOGONALIZE_B:
                model.B.weight.data = gram_schmidt(model.B.weight.data)
        # ===================log========================
        print('epoch [{}/{}], loss:{:.4f}, completed at {}'
            .format(epoch+1, NUM_EPOCHS, loss.data.item(), datetime.now()))
        loss_epochs.append(loss)
        save_state("./model_state.pth", model, optimizer, loss_epochs)
        np.savetxt("./convergence_loss.csv", loss_epochs, delimiter=',')

    plt.plot(range(1,len(loss_epochs)+1), loss_epochs)
    plt.title("Convergence of loss for full model")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.savefig("./convergence_plot.png")


def main_ae(dataloader):
    print("Training Voice Autoencoder model. ", datetime.now())
    AE_model = Voice_Autoencoder()
    if CUDA_AVAIL:
        AE_model = AE_model.cuda()
    AE_optimizer = torch.optim.Adam(AE_model.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)

    AE_loss_epochs = []
    for epoch in range(AE_NUM_EPOCHS):
        for batch in dataloader:
            # ===================forward=====================
            voice_data, IDs = batch
            voice_outputs, w = AE_model(voice_data)
            loss = voice_loss(voice_outputs, voice_data)
            # ===================backward====================
            AE_optimizer.zero_grad()
            loss.backward()
            AE_optimizer.step()
        # ===================log========================
        print('epoch [{}/{}], loss:{:.4f}, completed at {}'
            .format(epoch+1, AE_NUM_EPOCHS, loss.data.item(), datetime.now()))
        AE_loss_epochs.append(loss)
        save_state("./AE_model_state.pth", AE_model, AE_optimizer, AE_loss_epochs)
        np.savetxt("./AE_convergence_loss.csv", AE_loss_epochs, delimiter=',')
    
    plt.plot(range(1,len(AE_loss_epochs)+1), AE_loss_epochs)
    plt.title("Convergence of loss for AE model")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.savefig("./AE_convergence_plot.png")


def save_state(path, model, optimizer, loss): # epoch, loss
    torch.save({
            'model': str(model),
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            #'epoch': epoch,
            'loss': loss,
            'w_length': model.w_length
            }, path)

def load_state(path, model, optimizer, print_model=True): # epoch, loss
    checkpoint = torch.load(path)
    model.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    model.w_length = checkpoint['w_length']
    if(print_model == True):
        model_state = checkpoint['model']
        print(model_state)
    #epoch = checkpoint['epoch']
    #loss = checkpoint['loss']

def view_state(model, optimizer, state_size = 0):
    if(state_size == 1):
        print("Model -",model)
        return
    print("Model's state_dict:")
    for param_tensor in model.state_dict():
        print(param_tensor, "\t", model.state_dict()[param_tensor])
    print("Optimizer's state_dict:")
    for var_name in optimizer.state_dict():
        print(var_name, "\t", optimizer.state_dict()[var_name])

def gram_schmidt(vv):
    def projection(u, v):
        return (v * u).sum() / (u * u).sum() * u

    nk = vv.size(1) # debugged from original repo
    uu = torch.zeros_like(vv, device=vv.device)
    uu[:, 0] = vv[:, 0].clone() # copy first column
    for k in range(1, nk):
        vk = vv[:, k].clone() # debugged from original repo
        uk = 0
        for j in range(0, k): # project vk onto space spanned by bases so far
            uj = uu[:, j].clone()
            uk = uk + projection(uj, vk)
        uu[:, k] = vk - uk
    for k in range(nk):
        uk = uu[:, k].clone()
        uu[:, k] = uk / uk.norm()
    return uu
# source: https://github.com/legendongary/pytorch-gram-schmidt

class DeviceDataloader():
    """Wrap a dataloader to move data to a device"""
    def __init__(self, dl, device):
        self.dl = dl
        self.device = device
    def __iter__(self):
        """Yield a batch of data after moving it to device"""
        for b in self.dl:
            yield to_device(b, self.device)
    def __len__(self):
        """Number of batches"""
        return len(self.dl)
# source: https://medium.com/dsnet/training-deep-neural-networks-on-a-gpu-with-pytorch-11079d89805

def to_device(data, device):
    """Move tensor(s) to chosen device"""
    if isinstance(data, (list,tuple)):
        return [to_device(x, device) for x in data]
    return data.to(device, non_blocking=True)
# source: https://medium.com/dsnet/training-deep-neural-networks-on-a-gpu-with-pytorch-11079d89805


## helper routine
def conv_shape(L, K, S, P):
    return (L + 2*P - K) // S + 1











"""V2F_eval_task.py"""
# import numpy as np
from scipy.linalg import sqrtm
# import matplotlib.pyplot as plt
# import torch
# from datetime import datetime

def evaluate_model(model, evaluate_IDs, voice_eval_path, face_dict, lineup_length=10, top_n=10, save=True):
    """
    Evaluates the face predictions for a given model with a line up task.
    The task is repeated for every ID given in the evaluate_IDs list, and a list
    of the rank of the real_face in each task is returned. See the function 
    lineup for details of the task.
    INPUTS
    - model:            a given model with a model.predict method that takes an 
                        ID and voice pathname and returns a face reconstruction
    - evaluate_IDs:     a list of IDs to evaluate
    - voice_eval_path:       the pathname where the voice spectrograms of the given 
                        evaluate_IDs can be found, in csv form
    - face_dict:        a dictionary of faces, where the key is the ID as an int
                        and the value is a 2D numpy matrix of the face pixels
    - lineup_length:    the number of IDs to place in the lineup for the task
    """
    if type(evaluate_IDs) == torch.Tensor:
        evaluate_IDs = evaluate_IDs.tolist()
    
    N = len(evaluate_IDs)

    ranks = []
    for i, ID in enumerate(evaluate_IDs):
        face_reconstr = model.predict(ID, voice_eval_path)
        rank, error = lineup(face_reconstr, ID, face_dict, lineup_length=lineup_length)
        ranks.append(rank)
        print("Evaluation number {} of {}: ID={} was rank {}/{}. {}".format(
            i+1, N, ID, rank, lineup_length, datetime.now())
        )

    top_n_acc = top_n_accuracy(ranks, top_n)
    if save:
        np.savetxt("./ranks.csv", ranks, delimiter=',')
        np.savetxt("./top_n_accuracy.csv", top_n_acc, delimiter=',')
    return top_n_acc

def plot_top_n_acc(top_n_acc, save=True):
    n_range = range(1, len(top_n_acc)+1)
    plt.plot(n_range, top_n_acc)
    plt.title("Model Top-n Accuracy on Evaluation Dataset")
    plt.ylabel("Top-n Accuracy")
    plt.xlabel("n")
    plt.xticks(n_range)
    if save:
        plt.savefig("./top_n_accuracy.png")


def top_n_accuracy(ranks, n):
    """
    INPUTS
    - ranks:    1D numpy array of ranks
    - n:        the largest top_n to output
    OUTPUTS
    - np.array  an array of top_n_accuracy where the n'th element is the top_n_accuracy
    """
    if type(ranks) == list:
        ranks = np.array(ranks)
    lineup_length = ranks.size
    n = min(n, lineup_length) # check input isn't erroneous
    
    top_n_accuracy = []
    for i in range(1, n+1):
        top_i_accuracy = np.where(ranks <= i, 1, 0).sum() / lineup_length
        top_n_accuracy.append(top_i_accuracy)
    return np.array(top_n_accuracy)

def lineup(face_reconstr, real_id, face_dict, lineup_length=10):
    """
    Note: includes the training faces in the lineup pool that is sampled from
    """
    assert(lineup_length <= len(face_dict))

    # Set up array of face_ids for the lineup
    other_IDs = list(face_dict.keys())
    other_IDs.remove(real_id) # all IDs except real_id
    others = np.random.choice(other_IDs, size=lineup_length-1, replace=False) # randomly sample
    line_up = np.concatenate(([real_id], others)) # real_id is line_up[0]

    # Calculate the FID between each real face and the reconstructed face
    errors = np.zeros(lineup_length)
    for i in range(lineup_length):
        ID = line_up[i]
        errors[i] = fid(face_reconstr, face_dict[ID])
    
    # minimum FID means the reconstructed face is closest to real face
    order = np.argsort(errors)
    #change from 0-indexing to 1-indexing
    rank = order[0]+1
    return rank, errors[0]


def fid(act1, act2):
    """
    calculate frechet inception distance
    """
    # calculate mean and covariance statistics
    mu1 = act1.mean(axis=0)
    sigma1 = np.cov(act1, rowvar=False)
    mu2 = act2.mean(axis=0)
    sigma2 = np.cov(act2, rowvar=False)
    
    # calculate sum squared difference between means
    ssdiff = np.sum((mu1 - mu2)**2.0)
    
    # calculate sqrt of product between cov
    covmean = sqrtm(sigma1.dot(sigma2))
    
    # check and correct imaginary numbers from sqrt
    if np.iscomplexobj(covmean):
        covmean = covmean.real
    
    # calculate score
    fid = ssdiff + np.trace(sigma1 + sigma2 - 2.0 * covmean)
    return fid


validation_IDs = [
    285,
    397,
    376,
    223,
    836,
    175,
    315,
    804,
    106,
    758,
    338,
    906,
    528,
    565,
    198,
    379,
    854,
    207,
    123,
    173,
    828,
    431,
    399,
    407,
    834,
    621,
    339,
    1019,
    481,
    75,
    722,
    684,
    888,
    150,
    554,
    373,
    250,
    922,
    824,
    649,
    24,
    874,
    111,
    161,
    426,
    231,
    765,
    138,
    665,
    976,
    717,
    844,
    421,
    840,
    596,
    883,
    872,
    141,
    122,
    672,
    369,
    500,
    549,
    246,
    964,
    474,
    879,
    191,
    774,
    690,
    618,
    208,
    303,
    319,
    255,
    363,
    856,
    954,
    81,
    179,
    181,
    486,
    851,
    417,
    293,
    275,
    88,
    186,
    290,
    447,
    322,
    746,
    288,
    489,
    1000,
    335,
    380,
    763,
    4,
    236,
    264,
    778,
    251,
    981,
    449,
    158,
    291,
    331,
    741,
    98,
    151,
    96,
    996,
    783,
    555,
    865,
    661,
    425,
    333,
    928,
    65,
    685,
    611,
    675,
    1016,
    71,
    873,
    762,
    697,
    38,
    218,
    992,
    299,
    196,
    584,
    295,
    701,
    930,
    359,
    269,
    225,
    213,
    120,
    558,
    531,
    229,
    583,
    348,
    433,
    936,
    253,
    435,
    346
]

main()