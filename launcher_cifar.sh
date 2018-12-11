#!/usr/bin/env bash


python -u train.py --problem cifar10 --pmap 1 --n_batch_train 32 --optimizer adam --lr 1e-4 --image_size 32 --depth 32 --n_levels 1 --width 400 --flow_permutation 2 --flow_coupling 1 --n_train 640 --seed 42