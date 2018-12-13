#!/usr/bin/env bash

python -u train.py \
--problem cifar10 \
--n_batch_init 32 \
--pmap 1 \
--n_batch_train 32 \
--n_batch_init 32 \
--n_batch_test 32 \
--optimizer adam \
--lr 1e-4 \
--image_size 32 \
--depth 26 \
--n_levels 2 \
--width 512 \
--flow_permutation 2 \
--flow_coupling 1 \
--n_train 5000 \
--seed 42 \
--logdir logs \
--dal 1 \
--epochs_warmup 5
&> train_out.txt