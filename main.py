# -*- coding: utf-8 -*-
"""
Created on Thu Oct 12 16:19:40 2023

@author: farismismar
"""

import numpy as np
import pandas as pd
from scipy.constants import pi, c
import matplotlib.pyplot as plt
from sklearn.cluster import KMeans
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
import tikzplotlib

import pdb

import os
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

# For Windows users
if os.name == 'nt':
    os.add_dll_directory("/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v11.6/bin")

import tensorflow as tf
# print(tf.config.list_physical_devices('GPU'))

# The GPU ID to use, usually either "0" or "1" based on previous line.
os.environ["CUDA_VISIBLE_DEVICES"] = "0" 

from tensorflow import keras
from tensorflow.keras import layers, optimizers
from tensorflow.compat.v1 import set_random_seed

from sklearn.preprocessing import MinMaxScaler

# System parameters
file_name = 'faris.bmp' # either a file name or a payload
payload_size = 0 # 30000 # bits
constellation = 'QAM'
M_constellation = 64
MIMO_equalizer = 'MMSE'
seed = 7
codeword_size = 1024 # bits
n_pilot = 4
N_r = 4
N_t = 4
f_c = 1.8e6 # in Hz
quantization_b = np.inf
shadowing_std = 8  # dB

crc_polynomial = 0b1001_0011
crc_length = 24 # bits

Tx_SNRs = [3, 10, 20, 30, 40, 45, 50, 60, 70, 80] # in dB

prefer_gpu = True
##################

__release_date__ = '2024-01-13'
__ver__ = '0.4'

##################
plt.rcParams['font.family'] = "Arial"
plt.rcParams['font.size'] = "14"

np_random = np.random.RandomState(seed=seed)
k_constellation = int(np.log2(M_constellation))


def create_constellation(constellation, M):
    if (constellation == 'PSK'):
        return _create_constellation_psk(M)
    elif (constellation == 'QAM'):
        return _create_constellation_qam(M)
    else:
        return None


# Constellation based on Gray code
def _create_constellation_psk(M):
    k = np.log2(M)
    if k != int(k): # only square constellations are allowed.
        print('Only square PSK constellations allowed.')
        return None

    k = int(k)
    constellation = pd.DataFrame(columns=['m', 'x_I', 'x_Q'])

    for m in np.arange(M):
        centroid_ = pd.DataFrame(data={'m': int(m),
                                       'x_I': np.sqrt(1 / 2) * np.cos(2*np.pi/M*m + np.pi/M),
                                       'x_Q': np.sqrt(1 / 2) * np.sin(2*np.pi/M*m + np.pi/M)}, index=[m])
        if constellation.shape[0] == 0:
            constellation = centroid_.copy()
        else:
            constellation = pd.concat([constellation, centroid_], ignore_index=True)
    
    gray = constellation['m'].apply(lambda x: decimal_to_gray(x, k))
    constellation['I'] = gray.str[:(k//2)]
    constellation['Q'] = gray.str[(k//2):]

    constellation.loc[:, 'x'] = constellation.loc[:, 'x_I'] + 1j * constellation.loc[:, 'x_Q']
    
    # Normalize the transmitted symbols    
    # The average power is normalized to unity
    P_average = np.mean(np.abs(constellation.loc[:, 'x']) ** 2)
    constellation.loc[:, 'x'] /= np.sqrt(P_average)
    
    return constellation


# Constellation based on Gray code
def _create_constellation_qam(M):
    k = np.log2(M)
    if k != int(k): # only square QAM is allowed.
        print('Only square QAM constellations allowed.')
        return None

    k = int(k)
    m = np.arange(M)
    Am_ = np.arange(-np.sqrt(M) + 1, np.sqrt(M), step=2, dtype=int) # Proakis p105
    
    Am = np.zeros(M, dtype=np.complex64)
    idx = 0
    for Am_I in Am_:
        for Am_Q in Am_:
            Am[idx] = Am_I + 1j * Am_Q
            idx += 1
    
    # This will hold the transmitted symbols
    constellation = pd.DataFrame(data={'x_I': np.real(Am),
                                       'x_Q': np.imag(Am)})
    constellation.insert(0, 'm', m)
    constellation_ordered = pd.DataFrame()
    for idx, s in enumerate(np.array_split(constellation, int(np.sqrt(M)))):
        if idx % 2 == 1:
            s = s.iloc[::-1] # Invert 
        # print(s)
        constellation_ordered = pd.concat([constellation_ordered, s], axis=0)
    
    constellation = constellation_ordered.copy()
    constellation = constellation.reset_index(drop=True)
    constellation['m'] = constellation.index
    
    gray = constellation['m'].apply(lambda x: decimal_to_gray(x, k))
    constellation['I'] = gray.str[:(k//2)]
    constellation['Q'] = gray.str[(k//2):]
    
    constellation.loc[:, 'x'] = constellation.loc[:, 'x_I'] + 1j * constellation.loc[:, 'x_Q']
    
    # Normalize the transmitted symbols    
    # The average power is normalized to unity
    P_average = np.mean(np.abs(constellation.loc[:, 'x']) ** 2)
    constellation.loc[:, 'x'] /= np.sqrt(P_average)
    
    return constellation


def quantize(x, b):
    if b == np.inf:
        return x
        
    m, n = x.shape
    x = x.flatten()
    
    x_re = np.real(x)
    x_im = np.imag(x)

    x_re_b = _lloyd_max_quantization(x_re, b)
    x_im_b = _lloyd_max_quantization(x_im, b)
    
    x_b = x_re_b + 1j * x_im_b
    
    return x_b.reshape((m, n))


def _lloyd_max_quantization(x, b, max_iteration=100):
    # derives the quantized vector
    # https://gist.github.com/PrieureDeSion
    # https://github.com/stillame96/lloyd-max-quantizer
    from utils import normal_dist, expected_normal_dist, MSE_loss, LloydMaxQuantizer
    
    repre = LloydMaxQuantizer.start_repre(x, b)
    min_loss = 1.0

    for i in range(max_iteration):
        thre = LloydMaxQuantizer.threshold(repre)
        # In case wanting to use with another mean or variance,
        # need to change mean and variance in utils.py file
        repre = LloydMaxQuantizer.represent(thre, expected_normal_dist, normal_dist)
        x_hat_q = LloydMaxQuantizer.quant(x, thre, repre)
        loss = MSE_loss(x, x_hat_q)

        # # Print every 10 loops
        # if(i%10 == 0 and i != 0):
        #     print('iteration: ' + str(i))
        #     print('thre: ' + str(thre))
        #     print('repre: ' + str(repre))
        #     print('loss: ' + str(loss))
        #     print('++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')

        # Keep the threhold and representation that has the lowest MSE loss.
        if(min_loss > loss):
            min_loss = loss
            min_thre = thre
            min_repre = repre

    # print('min loss: ' + str(min_loss))
    # print('min thresholds: ' + str(min_thre))
    # print('min representative levels: ' + str(min_repre))
    
    # x_hat_q with the lowest amount of loss.
    best_x_hat_q = LloydMaxQuantizer.quant(x, min_thre, min_repre)
    
    return best_x_hat_q


def decimal_to_gray(n, k):
    gray = n ^ (n >> 1)
    gray = bin(gray)[2:]
    
    return '{}'.format(gray).zfill(k)


def _plot_constellation(constellation):
    fig, ax = plt.subplots(1, 1, figsize=(8, 5))
    plt.scatter(constellation['x_I'], constellation['x_Q'], c='k', marker='o', lw=2)
    for idx, row in constellation.iterrows():
        x, y = row[['x_I', 'x_Q']]
        if y < 0:
            yshift = -0.1
        else:
            yshift = 0.05
        
        ax.text(row['x_I'], row['x_Q'] + yshift, s='{}{}'.format(row['I'], row['Q']))
        
    plt.grid(True)
    plt.xlabel('I')
    plt.ylabel('Q')
    plt.show()
    tikzplotlib.save('constellation.tikz')
    plt.close(fig)
    

def _ber(a, b):
    assert(len(a) == len(b))
    
    length = len(a)
    bit_error = 0
    for idx in range(length):
        if a[idx] != b[idx]:
            bit_error += 1
            
    return bit_error / length


def _vec(A):
    # A is numpy array
    return A.flatten(order='F')

    
def bits_to_symbols(x_b, alphabet, k):
    iter_ct = int(np.ceil(len(x_b) / k))
    
    df_ = alphabet[['m', 'I', 'Q']].copy()
    df_.loc[:, 'IQ'] = df_['I'].astype(str) + df_['Q'].astype(str)
    
    x_sym = []
    for i in range(iter_ct):
        bits_i = x_b[i*k:(i+1)*k] # read the next ith stride of k bits
        # Convert this to symbol from alphabet
        sym_i = df_.loc[df_['IQ'] == bits_i.zfill(k), 'm'].values[0].astype(int)
        # print(bits_i, sym_i)
        x_sym.append(sym_i)
        
    return np.array(x_sym)


def symbols_to_bits(x_sym, k, alphabet, is_complex=False):    
    if is_complex == False: # Here, symbols are given by number, not by I/Q
        x_bits = ''
        for s in x_sym:
            try:
                i, q = alphabet.loc[alphabet['m'] == s, ['I', 'Q']].values[0]
            except:
                # There is no corresponding I/Q, so these are padding, simply append with X
                i, q = 'X', 'X'
                pass
            x_bits += '{}{}'.format(i, q).zfill(k)
        return x_bits
    else:
        # Convert the symbols to number first, then call the function again
        information = []
        x_sym_IQ = x_sym
        # m, n = x_sym_IQ.shape
        x_sym_IQ = x_sym_IQ.flatten()
        for s in x_sym_IQ:
            try:
                information.append(alphabet[np.isclose(alphabet['x'], s)]['m'].values[0])
            except:
                information.append('X')
                pass
        information = np.array(information)
        return symbols_to_bits(information, k, alphabet, is_complex=False)


def bits_to_baseband(x_bits, alphabet, k):    
    x_b_i = []
    x_b_q = []
    for idx in range(len(x_bits) // k):
        codeword = x_bits[idx*k:(idx+1)*k]    
        x_b_i.append(codeword[:(k//2)])
        x_b_q.append(codeword[(k//2):])

    x_sym = []
    # Next is baseband which is the complex valued symbols
    for i, q in zip(x_b_i, x_b_q):
        sym = alphabet.loc[(alphabet['I'] == i) & (alphabet['Q'] == q), 'x'].values[0]
        x_sym.append(sym)
        
    x_sym = np.array(x_sym)
    
    return x_b_i, x_b_q, x_sym


def compute_crc(x_bits_orig, crc_polynomial, crc_length):
    # Introduce CRC to x
    global codeword_size
    
    # Make sure the crc polynomial is not longer than the codeword size.
    # Otherwise, an error
    length_crc = len(bin(crc_polynomial)[2:])
    
    if codeword_size < length_crc:
        raise ValueError(f'The codeword size should be {length_crc} bits')
        
    x_bits = x_bits_orig.zfill(codeword_size)

    crc = 0
    for position, value in enumerate(bin(crc_polynomial)[2:]):
        if value == '1':
            crc = crc ^ int(x_bits[position])
    crc = bin(crc)[2:]
    
    if len(crc) > crc_length:
        raise ValueError("Check CRC length parameter.")
    crc = crc.zfill(crc_length)
    
    return crc


def create_ricean_channel(N_r, N_t, K, sigma_dB=8):
    global G # Pathloss in dB
    
    G_fading = dB(G) - np_random.normal(loc=0, scale=np.sqrt(sigma_dB), size=(N_r, N_t))
    G_fading = np.array([linear(g) for g in G_fading])
    
    mu = np.sqrt(K / (1 + K))
    sigma = np.sqrt(1 / (1 + K))
    
    # Rician fading
    H = np.sqrt(G / 2) * np_random.normal(loc=mu, scale=sigma, size=(N_r, N_t)) + \
        1j * np_random.normal(loc=mu, scale=sigma, size=(N_r, N_t))
    
    return H


def create_rayleigh_channel(N_r, N_t, sigma_dB=8):
    global G # Pathloss in dB
    
    G_fading = dB(G) - np_random.normal(loc=0, scale=np.sqrt(sigma_dB), size=(N_r, N_t))
    G_fading = np.array([linear(g) for g in G_fading])
    
    # Rayleigh fading with G being the large scale fading
    H = np.sqrt(G / 2) * (np_random.normal(0, 1, size=(N_r, N_t)) + \
                          1j * np_random.normal(0, 1, size=(N_r, N_t)))
    
    # TODO:  if fading then add to H the coefficients.
    return H


def _loss_fn_classifier(Y_true, Y_pred):
    cce = tf.keras.losses.CategoricalCrossentropy()
    return cce(Y_true, Y_pred)


def _create_dnn(input_dimension, output_dimension, depth=5, width=10):
    nX = input_dimension
    nY = output_dimension
    
    model = keras.Sequential()
    model.add(keras.Input(shape=(input_dimension,)))
    
    for hidden in range(depth):
        model.add(layers.Dense(width, activation='sigmoid'))
   
    model.add(layers.Dense(nY, activation='softmax'))
    
    model.compile(loss=_loss_fn_classifier, optimizer='adam', 
                  metrics=['accuracy', 'categorical_crossentropy']) # Accuracy here is okay.
    
    # Reporting the number of parameters
    print(model.summary())
    
    num_params = model.count_params()
    print('Number of parameters: {}'.format(num_params))
    
    return model


def DNN_detect_symbol(X, y, train_split=0.8, depth=5, width=2, epoch_count=100, batch_size=32):
    global prefer_gpu
    global np_random
    
    use_cuda = len(tf.config.list_physical_devices('GPU')) > 0 and prefer_gpu
    device = "/gpu:0" if use_cuda else "/cpu:0"

    _, nX = X.shape
    
    X_train, X_test, y_train, y_test = \
        train_test_split(X, y, train_size=train_split,
                    random_state=np_random)
  
    le = LabelEncoder()
    le.fit(y_train)
    encoded_y = le.transform(y_train)     
    Y_train = keras.utils.to_categorical(encoded_y)
    encoded_y = le.transform(y_test)
    Y_test = keras.utils.to_categorical(encoded_y)
    
    _, nY = Y_train.shape

    dnn_classifier = _create_dnn(input_dimension=nX, output_dimension=nY,
                                 depth=depth, width=width)
    
    with tf.device(device):
        dnn_classifier.fit(X_train, Y_train, epochs=epoch_count, batch_size=batch_size)
        
    with tf.device(device):
        Y_pred = dnn_classifier.predict(X_test)
        loss, accuracy_score, _ = dnn_classifier.evaluate(X_test, Y_test)

    # Reverse the encoded categories
    y_test = le.inverse_transform(np.argmax(Y_test, axis=1))
    y_pred = le.inverse_transform(np.argmax(Y_pred, axis=1))
      
    return dnn_classifier, accuracy_score, np.c_[y_test, y_pred]


def _unsupervised_detection(x_sym_hat, alphabet):
    global np_random, M_constellation
    
    x_sym_hat_flat = x_sym_hat.flatten()

    X = np.real(x_sym_hat_flat)
    X = np.c_[X, np.imag(x_sym_hat_flat)]
    X = X.astype('float32')
    
    centroids = alphabet[['x_I', 'x_Q']].values
    centroids = centroids.astype('float32')
    
    # Intialize k-means centroid location deterministcally as a constellation
    kmeans = KMeans(n_clusters=M_constellation, init=centroids, n_init=1, 
                    random_state=np_random).fit(centroids)
    
    information = kmeans.predict(X).reshape(x_sym_hat.shape)
    df_information = pd.DataFrame(data={'m': information})
    
    df = df_information.merge(alphabet, how='left', on='m')
    symbols = df['x'].values.reshape(x_sym_hat.shape)
    bits_i = df['I'].values.reshape(x_sym_hat.shape)
    bits_q = df['Q'].values.reshape(x_sym_hat.shape) 
    
    bits = []
    for s in range(x_sym_hat_flat.shape[0]):
        bits.append(f'{bits_i[s]}{bits_q[s]}')
        
    bits = np.array(bits).reshape(x_sym_hat.shape)    

    return information, symbols, [bits_i, bits_q], bits


def ML_detect_symbol(symbols, alphabet):    
    df_information = pd.DataFrame()
    symbols_flat = symbols.flatten()
    
    for s in range(symbols_flat.shape[0]):
        x_hat = symbols_flat[s]
        # This function returns argmin |x - s_m| based on AWGN ML detection
        # for any arbitrary constellation denoted by the alphabet
        distances = alphabet['x'].apply(lambda x: np.abs(x - x_hat) ** 2)
        
        # Simple distances.idxmin is not cutting it.
        m_star = distances.idxmin(axis=0)
        
        df_i = pd.DataFrame(data={'m': m_star,
                                  'x': alphabet.loc[alphabet['m'] == m_star, 'x'],
                                  'I': alphabet.loc[alphabet['m'] == m_star, 'I'],
                                  'Q': alphabet.loc[alphabet['m'] == m_star, 'Q']})
        
        df_information = pd.concat([df_information, df_i], axis=0, ignore_index=True)
    
    information = df_information['m'].values.reshape(symbols.shape)
    
    # Now simply compute other elements.
    symbols = df_information['x'].values.reshape(symbols.shape)
    bits_i = df_information['I'].values
    bits_q = df_information['Q'].values
    
    bits = []
    for s in range(symbols_flat.shape[0]):
        bits.append(f'{bits_i[s]}{bits_q[s]}')
        
    bits = np.array(bits).reshape(symbols.shape)
    bits_i = bits_i.reshape(symbols.shape)
    bits_q = bits_q.reshape(symbols.shape)
    
    return information, symbols, [bits_i, bits_q], bits
    

def equalize_channel(H_hat, algorithm, rho=None):
    # rho is linear (non dB).  So is Rx_SNR.
    
    N_r, N_t = H_hat.shape
    
    if algorithm == 'ZF':
        W = np.linalg.pinv(H_hat)
        Rx_SNR = rho / np.diag(np.real(np.linalg.inv(H_hat.conjugate().T@H_hat)))
    if algorithm == 'MMSE':
        assert(rho is not None)
        W = H_hat.conjugate().T@(np.linalg.inv(H_hat@H_hat.conjugate().T + (1./rho)*np.eye(N_t)))
        #Rx_SNR = 1 / np.diag(np.real(np.linalg.inv(rho * np.linalg.inv(H_hat.conjugate().T@H_hat) + np.eye(N_t)))) - 1
        WWH = W@W.conjugate().T
        Rx_SNR = rho / np.real(np.diag(WWH))

    assert(W.shape == (N_r, N_t))
    
    return W, Rx_SNR

        
def estimate_channel(X_p, Y_p, noise_power, algorithm, random_state=None):
    # This is for least square (LS) estimation    
    N_t, _ = X_p.shape
    
    if not np.allclose(X_p@X_p.T, np.eye(N_t)):
        raise ValueError("The training sequence is not semi-unitary.  Cannot estimate the channel.")
    
    if algorithm == 'LS':
        # This is least square (LS) estimation
        H_hat = Y_p@X_p.conjugate().T
    
    return H_hat
  

def generate_pilot(N_r, N_t, n_pilot, random_state=None):
    # Check if the dimensions are valid for the operation
    if n_pilot < N_t:
        raise ValueError("The length of the training sequence should be greater than or equal to the number of transmit antennas.")
    
    # Compute a unitary matrix from a combinatoric of e
    I = np.eye(N_t)
    idx = random_state.choice(range(N_t), size=N_t, replace=False)
    Q = I[:, idx] 
    
    assert(np.allclose(Q@Q.T, np.eye(N_t)))  # Q is indeed unitary, but square.
    
    # # Scale the unitary matrix
    # Q /= np.linalg.norm(Q, ord='fro')
    
    # To make a semi-unitary, we need to post multiply with a rectangular matrix
    # Now we need a rectangular matrix (fat)
    A = np.zeros((N_t, n_pilot), int)
    np.fill_diagonal(A, 1)
    X_p =  Q @ A
    
    # The pilot power should be SNR / noise power
    # What matrix X_pX_p* = I (before scaling)
    assert(np.allclose(X_p@X_p.T, np.eye(N_t)))  # This is it
    
    # The training sequence is X_p.  It has N_t rows and n_pilot columns
    return X_p
    

def channel_eigenmodes(H):
    # HH = H@H.conjugate().T
    # eigenvalues, eigenvectors = np.linalg.eig(HH)
    
    U, S, Vh = np.linalg.svd(H, full_matrices=False)
    eigenmodes = S ** 2
    return eigenmodes

    
def transmit_receive(data, codeword_size, alphabet, H, equalizer, snr_dB, crc_polynomial, crc_length, n_pilot, perfect_csi=False):
    global quantization_b
    
    k = np.log2(alphabet.shape[0]).astype(int)
    rho = linear(snr_dB)
    
    if codeword_size < k:
        raise ValueError("Codeword size is too small for the chosen modulation")
    
    if codeword_size / k != codeword_size // k:
        codeword_size = int(np.ceil(codeword_size / k)) * k
        print(f"WARNING: Codeword size is not an integer multiple of {k}.  Revising it to {codeword_size} bits.")
    
    SERs = []
    BERs = []
    block_error = 0
    
    N_r, N_t = H.shape
    
    Df = 15e3 # subcarrier in Hz
    tti = 1e-3 # in seconds
    
    ## Effective codeword size, must coincide with integer number of symbols
    # codeword_size = int(np.ceil(codeword_size / k) * k)
    
    # Bit rate 
    bit_rate = codeword_size / tti

    # Number of streams
    N_s = min(N_r, N_t)
    bit_rate_per_stream = bit_rate / N_s

    eig = channel_eigenmodes(H)
    n_eig = len(eig)
    
    print(f'Channel H has {n_eig} eigenmodes: {eig}.')
    print(f'Transmission maximum bitrate per stream = {bit_rate_per_stream:.2f} bps')
    
    # Find the correct number of subcarriers required for this bit rate
    # assuming 1:1 code rate.
    Nsc = int(np.ceil(bit_rate_per_stream / (k * Df)))    # Number of OFDM subcarriers
    B = Df # per OFDM resource element
    print(f'Transmission BW per stream = {B:.2f} Hz')
    print(f'Number of OFDM subcarriers per stream = {Nsc}')
    
    # Note that bit_rate / B cannot exceed the Shannon capacity
    # Thus if bandwidth became B N_s due to spatial multiplexing, then the bit rate also scales by N_s.
    C = bit_rate_per_stream / (Nsc * Df)  # Shannon capacity
    
    b = len(data)
    n_transmissions = int(np.ceil(b / (codeword_size * N_s)))
    
    x_info_complete = bits_to_symbols(data, alphabet, k)
    
    # Pilot symbols
    P = generate_pilot(N_r, N_t, n_pilot, random_state=np_random)
    
    SNR_Rx = []
    Tx_EbN0 = []
    Rx_EbN0 = []
    PL = []
    data_rx = []
    noise_powers = []
    channel_mse = []
    
    if b < 10000:
        print('Warning: Small number of bits can cause curves to differ from theoretical ones due to insufficient number of samples.')

    if n_transmissions < 10000:
        print('Warning: Small number of codeword transmissions can cause BLER values to differ from theoretical ones due to insufficient number of samples.')

    print(f'Transmitting a total of {b} bits.')
    for codeword in np.arange(n_transmissions):
        print(f'Transmitting codeword {codeword + 1}/{n_transmissions} at SNR {snr_dB} dB')
        # Every transmission is for one codeword, divided up to N_t streams.
        x_info = x_info_complete[codeword*N_t*(codeword_size // k):(codeword+1)*N_t*(codeword_size // k)]
        
        # 1) Compute CRC based on the original codeword
        # 2) Pad what is left *in between* to fulfill MIMO rank
        x_bits_orig = symbols_to_bits(x_info, k, alphabet)          # correct
        crc = compute_crc(x_bits_orig, crc_polynomial, crc_length)  # Compute CRC in bits.
        
        effective_crc_length = int(k * np.ceil(crc_length / k)) # The receiver is also aware of the CRC length (in bits)
        crc_padded = crc.zfill(effective_crc_length)
        _, _, crc_symbols = bits_to_baseband(crc_padded, alphabet, k)
        effective_crc_length_symbols = effective_crc_length // k # in symbols
                
        # Symbols
        x_b_i, x_b_q, x_sym = bits_to_baseband(x_bits_orig, alphabet, k)
        
        # Map codewords to the MIMO layers.
        pad_length = int(N_t * np.ceil((len(x_sym) + effective_crc_length_symbols) / N_t)) - len(x_sym) - effective_crc_length_symbols # in symbols
        x_sym_crc = np.r_[x_sym, np.zeros(pad_length), crc_symbols]
        
        # Signal energy
        x_sym_crc = x_sym_crc.reshape(-1, N_t).T # Do not be tempted to do the obvious!
        Ex = np.linalg.norm(x_sym_crc, ord=2, axis=0).mean()
        
        # Symbol power (OFDM resource element)
        P_sym_dB = dB(Ex * Df / N_t) + 30 # in dBm
        P_sym = linear(P_sym_dB)
        
        # Noise power                
        noise_power_dB = P_sym_dB - snr_dB # in dBm
        noise_powers.append(noise_power_dB)
        
        # Eb/N0 at the transmitter
        Tx_EbN0_ = snr_dB - dB(C)
        Tx_EbN0.append(Tx_EbN0_)

        print(f'Symbol power at the transmitter is: {P_sym_dB:.4f} dBm')
        print(f'Noise power at the transmitter is: {noise_power_dB:.4f} dBm')        
        print(f'EbN0 at the transmitter (per stream): {Tx_EbN0_:.4f} dB')
        
        if Tx_EbN0_ < -1.59:
            print('** Outage at the transmitter **')
        print()
                
        noise_power = linear(noise_power_dB)
        
        # TODO: Introduce a precoder
        F = np.eye(N_t)
        
        # Additive noise sampled from a complex Gaussian
        noise_dimension = max(n_pilot, x_sym_crc.shape[1])
        n = np_random.normal(0, scale=np.sqrt(noise_power)/np.sqrt(2), size=(N_r, noise_dimension)) + \
            1j * np_random.normal(0, scale=np.sqrt(noise_power)/np.sqrt(2), size=(N_r, noise_dimension))
        
        # Debug purposes
        if perfect_csi:
            H = np.eye(N_t)
                
        # Channel
        # Since the channel coherence time is assumed constant
        H = H # this line has no meaning except to remind us that the channel changes after coherence time.
        # since every transmission is one TTI = 1 ms.
        
        # Channel impact        
        Hx = H@x_sym_crc # Impact of the channel on the transmitted symbols
        Y = Hx + n[:, :x_sym_crc.shape[1]] * np.linalg.norm(Hx, ord=2) / np.sqrt(P_sym)
        HP = H@P # Impact of the channel on the pilot
        
        # Note that the noise power for the pilot has to be scaled to abide by the SNR_dB
        T = HP + n[:, :n_pilot] * np.linalg.norm(P, ord=2) / np.sqrt(P_sym)

        # Estimate the channel
        H_hat = estimate_channel(P, T, noise_power, algorithm='LS', random_state=np_random)        
        
        ########################################################################
        error_vector = _vec(H) - _vec(H_hat)
        
        channel_estimation_mse = np.linalg.norm(error_vector, 2) ** 2 / (N_t * N_r)
        print(f'Channel estimation MSE: {channel_estimation_mse:.4f}')
        print()
        channel_mse.append(channel_estimation_mse)

        # For future:  If channel MSE is greater than certain value, then CSI
        # knowledge scenarios kick in (i.e., CSI is no longer known perfectly to
        # both the transmitter/receiver).
        
        # Introduce quantization for both Y and Y pilot
        Y_orig = Y
        T_orig = T
        Y = quantize(Y_orig, quantization_b)
        T = quantize(T_orig, quantization_b)
    
        # MIMO Receiver
        # The received symbol power *before* equalization impact
        P_sym_rx = P_sym * np.linalg.norm(H_hat, ord='fro') ** 2
        P_sym_rx_dB = dB(P_sym_rx)
        Rx_SNR_ = dB(rho * np.linalg.norm(H_hat, ord='fro') ** 2)
        
        # Compute the path loss, which is basically the channel effect
        PL.append(P_sym_dB - P_sym_rx_dB)
        
        # Equalizer to remove channel effect
        # Channel equalization
        # Now the received SNR per antenna (per symbol) due to the receiver
        # SNR per antenna and SNR per symbol are the same thing technically.
        W, Rx_SNRs_eq = equalize_channel(H_hat, algorithm=equalizer, rho=rho)
        Rx_SNRs_eq = [dB(r) for r in Rx_SNRs_eq]
        
        # An optimal equalizer should fulfill WH_hat = I_{N_t}     
        # Thus z = x_hat + v 
        #        = x_hat + W n
        z = W@Y
        x_sym_crc_hat = z

        # Now how to extract x_hat from z?        
        # Detection to extract the signal from the signal plus noise mix.
        x_sym_hat = _vec(x_sym_crc_hat)[:-effective_crc_length_symbols] # payload including padding

        # Remove the padding, which is essentially defined by the last data not on N_t boundary
        if pad_length > 0:
            x_sym_hat = x_sym_hat[:-pad_length] # no CRC and no padding.
            
        # Now let us find the SNR and Eb/N0 *after* equalization
        P_sym_rx_eq = P_sym * np.linalg.norm(W@H_hat, ord='fro') ** 2
        received_noise_powers = noise_power * np.real(np.diag(W.conjugate().T@W)) # np.linalg.norm(W, ord='fro') ** 2
        
        # Find the average receive SNR (per signal)
        Rx_SNRs_ = [dB(P_sym_rx_eq) - dB(p) for p in received_noise_powers]
        
        # TODO: Note Rx_SNRs_ and Rx_SNRs_eq must be equal        
        ########################################################################

        # Now the received SNR per antenna (per symbol) due to the receiver        
        Rx_SNRs_ = [dB(x) for x in Rx_SNRs_]
        SNR_Rx.append(Rx_SNRs_)
        
        # Compute the average EbN0 at the receiver
        Rx_EbN0_ = dB(linear(Rx_SNR_) / C)
        Rx_EbN0.append(Rx_EbN0_)

        print(f'Average signal SNR at the receiver: {Rx_SNR_:.4f} dB')
        print('Symbol SNR at the receiver (per stream): {} dB'.format(Rx_SNRs_eq))
        print(f'EbN0 at the receiver (per stream): {Rx_EbN0_:.4f} dB')
        
        if Rx_EbN0_ < -1.59:
            print('** Outage at the receiver **')                
        print()
        
        # Detection of symbols (symbol star is the centroid of the constellation)  
        x_info_hat, _, _, x_bits_hat = ML_detect_symbol(x_sym_hat, alphabet)
        # x_info_hat, _, _, x_bits_hat = _unsupervised_detection(x_sym_hat, alphabet)
        x_bits_hat = ''.join(x_bits_hat)

        # # To test the performance of DNN in detecting symbols:
        # X = np.c_[np.real(x_sym_hat), np.imag(x_sym_hat)]
        # y = x_info_hat
        # model_dnn, dnn_accuracy_score, _ = DNN_detect_symbol(X=X, y=y)
                
        # Compute CRC on the received frame
        crc_comp = compute_crc(x_bits_hat, crc_polynomial, crc_length)
        
        ########################################################
        # Error statistics
        # Block error
        if int(crc) != int(crc_comp):
            block_error += 1
        
        symbol_error = 1 - np.mean(x_info_hat == x_info)
        
        SERs.append(symbol_error)

        x_hat_b = symbols_to_bits(x_info_hat, k, alphabet, is_complex=False)
        x_hat_b_i, x_hat_b_q, _ = bits_to_baseband(x_hat_b, alphabet, k)

        ber_i = _ber(x_hat_b_i, x_b_i) 
        ber_q = _ber(x_hat_b_q, x_b_q)
        
        # System should preserve the number of bits.
        assert(len(x_bits_orig) == len(x_bits_hat))
        
        data_rx.append(x_bits_hat)
        ber = np.mean([ber_i, ber_q])
        BERs.append(ber)        
        # for

    total_transmitted_bits = N_t * codeword_size * n_transmissions
    print(f"Total transmitted bits: {total_transmitted_bits} bits.")

    BLER = block_error / n_transmissions

    # Now extract from every transmission 
    data_rx_ = ''.join(data_rx)    
        
    return np.arange(n_transmissions), Ex, SERs, noise_powers, SNR_Rx, PL, BERs, BLER, Tx_EbN0, Rx_EbN0, channel_mse, bit_rate_per_stream, Nsc, data_rx_

    
def dB(x):
    return 10 * np.log10(x)


def linear(x):
    return 10 ** (x / 10.)


def read_bitmap(file, word_length=8):
    global payload_size
    
    if file is not None:
        # This is a 32x32 pixel image
        im_orig = plt.imread(file)
        
        im = im_orig.flatten()
        im = [bin(a)[2:].zfill(word_length) for a in im] # all fields when converted to binary need to have word length.
    
        im = ''.join(im) # This is now a string of bits

    else:
        # These lines are for random bits 
        im  = np_random.binomial(1, 0.5, size=payload_size)
        s = ''
        for a in im:
            s = s + str(a)
        im = s
    
    return im


def _convert_to_bytes_decimal(data, word_length=8):
    n = len(data) // word_length
    dim = int(np.sqrt(n / 3))
    
    data_vector = []
    for i in range(n):
        d = str(data[i*word_length:(i+1)*word_length])
        d = int(d, 2)
        # print(d)
        data_vector.append(d)
    
    data_vector = np.array(data_vector, dtype='uint8')
    # Truncate if needed
    data_vector = data_vector[:dim * dim * 3]

    # Now reshape
    data_vector = data_vector.reshape(dim, dim, 3)
    
    return data_vector


def _plot_bitmaps(data1, data2):
    fig, [ax1, ax2] = plt.subplots(nrows=1, ncols=2)
        
    ax1.imshow(_convert_to_bytes_decimal(data1))
    ax2.imshow(_convert_to_bytes_decimal(data2))
    
    ax1.axis('off')
    ax2.axis('off')
    
    plt.tight_layout()
    plt.show()
    plt.close(fig)
    
    
def generate_plot(df, xlabel, ylabel):
    cols = list(set([xlabel, ylabel, 'Tx_SNR']))
    df = df[cols]
    df_plot = df.groupby('Tx_SNR').mean().reset_index()

    fig, ax = plt.subplots(figsize=(9,6))
    ax.set_yscale('log')
    plt.plot(df_plot[xlabel].values, df_plot[ylabel].values, '--bo', alpha=0.7, 
             markeredgecolor='k', markerfacecolor='r', markersize=6)
    plt.grid()
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.tight_layout()
    tikzplotlib.save('output.tikz')
    plt.show()
    plt.close(fig)
    

def compute_large_scale_fading(d, f_c, G_t=1, G_r=1, pl_exp=2):
    global np_random
    
    l = c / f_c
    G = G_t * G_r * (l / (4 * pi * d)) ** pl_exp
    
    assert (G < 1)
    
    return G

        
def run_simulation(file_name, codeword_size, channel_matrix, equalizer, constellation, Tx_SNRs, crc_polynomial, crc_length, n_pilot):
    alphabet = create_constellation(constellation=constellation, M=int(2 ** k_constellation))
    _plot_constellation(constellation=alphabet)
    data = read_bitmap(file_name)

    # _plot_bitmaps(data, data)
    df_output = pd.DataFrame()
    for snr in Tx_SNRs:
        c_i, Ex_Tx_i, SER_i, noise_power_i, Rx_SNR_i, PL_i, BER_i, BLER_i, Tx_EbN0_i, Rx_EbN0_i, channel_mse_i, bit_rate, subcarriers, data_received = \
            transmit_receive(data, codeword_size, alphabet, channel_matrix, equalizer, 
                         snr, crc_polynomial, crc_length, n_pilot, perfect_csi=False)
        df_output_ = pd.DataFrame(data={'Codeword': c_i})
        df_output_['Ex'] = Ex_Tx_i
        df_output_['SER'] = SER_i
        df_output_['noise_power'] = noise_power_i
        df_output_['Tx_SNR'] = snr
        df_output_['Rx_SNR'] = Rx_SNR_i
        df_output_['PL'] = PL_i
        df_output_['Avg_BER'] = BER_i
        df_output_['BLER'] = BLER_i
        df_output_['Tx_EbN0'] = Tx_EbN0_i
        df_output_['Rx_EbN0'] = Rx_EbN0_i
        df_output_['Channel_MSE'] = channel_mse_i
        df_output_['Bit_Rate'] = bit_rate
        df_output_['N_subcarriers'] = subcarriers
        
        _plot_bitmaps(data, data_received)
        
        if df_output.shape[0] == 0:
            df_output = df_output_.copy()
        else:
            df_output = pd.concat([df_output_, df_output], axis=0, ignore_index=True)
        
    df_output = df_output.reset_index(drop=True)
    
    return df_output


# 1) Create a channel
G = compute_large_scale_fading(d=100, f_c=f_c)
# H = create_rayleigh_channel(N_r=N_r, N_t=N_t)
H = create_ricean_channel(N_r=N_r, N_t=N_t, K=0, sigma_dB=shadowing_std)

# 2) Run the simulation on this channel
df_output = run_simulation(file_name, codeword_size, H, MIMO_equalizer, 
                           constellation, Tx_SNRs, crc_polynomial, crc_length,
                           n_pilot)
df_output.to_csv('output.csv', index=False)

# 3) Generate plot
xlabel = 'Rx_EbN0'
ylabel = 'Avg_BER'

generate_plot(df=df_output, xlabel=xlabel, ylabel=ylabel)
