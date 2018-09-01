import time
import numpy as np
#from nltk.translate.bleu_score import sentence_bleu as score_func
from nltk.translate.gleu_score import sentence_gleu as score_func
#from utils import score_sentence as score_func

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.utils as torch_utils

import utils
import data_loader

def get_reward(y, y_hat, n_gram = 6):
    # This method gets the reward based on the sampling result and reference sentence.
    # For now, we uses GLEU in NLTK, but you can used your own well-defined reward function.
    # In addition, GLEU is variation of BLEU, and it is more fit to reinforcement learning.

    # Since we don't calculate reward score exactly as same as multi-bleu.perl, 
    # (especialy we do have different tokenization,) I recommend to set n_gram to 6.

    # |y| = (batch_size, length1)
    # |y_hat| = (batch_size, length2)

    scores = []

    # Actually, below is really far from parallized operations.
    # Thus, it may cause slow training.
    for b in range(y.size(0)):
        ref = []
        hyp = []
        for t in range(y.size(1)):
            ref += [str(int(y[b, t]))]
            if y[b, t] == data_loader.EOS:
                break

        for t in range(y_hat.size(1)):
            hyp += [str(int(y_hat[b, t]))]
            if y_hat[b, t] == data_loader.EOS:
                break

        # for nltk.bleu & nltk.gleu
        scores += [score_func([ref], hyp, max_len = n_gram) * 100.] ## 6개로 n_gram 값을 주었다. BPE까지 하면 더 잛게 쪼개지기 때문

        # for utils.score_sentence
        #scores += [score_func(ref, hyp, 4, smooth = 1)[-1] * 100.]
    scores = torch.FloatTensor(scores).to(y.device) # Score가 있는 Float 텐서를 y가 있던 디바이스로 보내라.
    # |scores| = (batch_size)

    return scores

def get_gradient(y, y_hat, criterion, reward = 1):
    # |y| = (batch_size, length)
    # |y_hat| = (batch_size, length, output_size)
    # |reward| = (batch_size)
    batch_size = y.size(0)

    # Before we get the gradient, multiply -reward for each sample and each time-step.
    y_hat = y_hat * -reward.view(-1, 1, 1).expand(*y_hat.size())

    # Again, multiply -1 because criterion is NLLLoss.
    log_prob = -criterion(y_hat.contiguous().view(-1, y_hat.size(-1)), y.contiguous().view(-1))
    log_prob.div(batch_size).backward()

    return log_prob

def train_epoch(model, criterion, train_iter, valid_iter, config, start_epoch = 1, others_to_save = None):
    current_lr = config.rl_lr

    highest_valid_bleu = -np.inf
    no_improve_cnt = 0

    # Print initial valid BLEU before we start RL.
    model.eval()
    total_reward, sample_cnt = 0, 0
    for batch_index, batch in enumerate(valid_iter):
        current_batch_word_cnt = torch.sum(batch.tgt[1])
        x = batch.src
        y = batch.tgt[0][:, 1:]
        batch_size = y.size(0)
        # |x| = (batch_size, length)
        # |y| = (batch_size, length)

        # feed-forward
        y_hat, indice = model.search(x, is_greedy = True, max_length = config.max_length)
        # |y_hat| = (batch_size, length, output_size)
        # |indice| = (batch_size, length)

        reward = get_reward(y, indice, n_gram = config.rl_n_gram)

        total_reward += float(reward.sum())
        sample_cnt += batch_size
        if sample_cnt >= len(valid_iter.dataset.examples):
            break
    avg_bleu = total_reward / sample_cnt
    print("initial valid BLEU: %.4f" % avg_bleu) # You can figure-out improvement.
    model.train() # Now, begin training.

    # Start RL
    for epoch in range(start_epoch, config.rl_n_epochs + 1):
        #optimizer = optim.Adam(model.parameters(), lr = current_lr)
        optimizer = optim.SGD(model.parameters(), lr = current_lr) # Default hyper-parameter is set for SGD.
        print("current learning rate: %f" % current_lr)
        print(optimizer)

        sample_cnt = 0
        total_loss, total_bleu, total_sample_count, total_word_count, total_parameter_norm, total_grad_norm = 0, 0, 0, 0, 0, 0
        start_time = time.time()
        train_bleu = np.inf

        for batch_index, batch in enumerate(train_iter):
            optimizer.zero_grad()

            current_batch_word_cnt = torch.sum(batch.tgt[1])
            x = batch.src  
            y = batch.tgt[0][:, 1:]  # 정답, 앞의 BOS를 뺀다. - 샘플링으로 진행하기 떄문?
            batch_size = y.size(0)
            # |x| = (batch_size, length)
            # |y| = (batch_size, length)

            # Take sampling process because set False for is_greedy.
            y_hat, indice = model.search(x, is_greedy = False, max_length = config.max_length)
            # Based on the result of sampling, get reward.
            _a_actor = get_reward(y, indice, n_gram = config.rl_n_gram)
            # |y_hat| = (batch_size, length, output_size) ## 샘플링하기 전의 Softmax 값
            # |indice| = (batch_size, length) ## Long Tensor - 샘플링을 통해 만들어지다..?
            # |q_actor| = (batch_size)

            # Take samples as many as n_samples, and get average rewards for them.
            # I figured out that n_samples = 1 would be enough.
            baseline = []
            with torch.no_grad():
                for i in range(config.n_samples):
                    _, sampled_indice = model.search(x, is_greedy = False, max_length = config.max_length)
                    baseline += [get_reward(y, sampled_indice, n_gram = config.rl_n_gram)]
                baseline = torch.stack(baseline).sum(dim = 0).div(config.n_samples)
                # |baseline| = (n_samples, batch_size) --> (batch_size)

            # Now, we have relatively expected cumulative reward.
            # Which score can be drawn from q_actor subtracted by baseline.
            tmp_reward = q_actor - baseline
            # |tmp_reward| = (batch_size)
            # calcuate gradients with back-propagation
            get_gradient(indice, y_hat, criterion, reward = tmp_reward)

            # simple math to show stats
            total_loss += float(tmp_reward.sum())
            total_bleu += float(q_actor.sum())
            total_sample_count += batch_size
            total_word_count += int(current_batch_word_cnt)
            total_parameter_norm += float(utils.get_parameter_norm(model.parameters()))
            total_grad_norm += float(utils.get_grad_norm(model.parameters()))

            if (batch_index + 1) % config.print_every == 0:
                avg_loss = total_loss / total_sample_count
                avg_bleu = total_bleu / total_sample_count
                avg_parameter_norm = total_parameter_norm / config.print_every
                avg_grad_norm = total_grad_norm / config.print_every
                elapsed_time = time.time() - start_time

                print("epoch: %d batch: %d/%d\t|param|: %.2f\t|g_param|: %.2f\trwd: %.4f\tBLEU: %.4f\t%5d words/s %3d secs" % (epoch, 
                                                                                                            batch_index + 1, 
                                                                                                            int(len(train_iter.dataset.examples) // config.batch_size), 
                                                                                                            avg_parameter_norm, 
                                                                                                            avg_grad_norm, 
                                                                                                            avg_loss,
                                                                                                            avg_bleu,
                                                                                                            total_word_count // elapsed_time,
                                                                                                            elapsed_time
                                                                                                            ))

                total_loss, total_bleu, total_sample_count, total_word_count, total_parameter_norm, total_grad_norm = 0, 0, 0, 0, 0, 0
                start_time = time.time()

                train_bleu = avg_bleu

            # In orther to avoid gradient exploding, we apply gradient clipping.
            torch_utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)
            # Take a step of gradient descent.
            optimizer.step()

            sample_cnt += batch_size
            if sample_cnt >= len(train_iter.dataset.examples):
                break

        sample_cnt = 0
        total_reward = 0

        # Start validation
        with torch.no_grad():
            model.eval() # Turn-off drop-out

            for batch_index, batch in enumerate(valid_iter):
                current_batch_word_cnt = torch.sum(batch.tgt[1])
                x = batch.src
                y = batch.tgt[0][:, 1:]
                batch_size = y.size(0)
                # |x| = (batch_size, length)
                # |y| = (batch_size, length)

                # feed-forward
                y_hat, indice = model.search(x, is_greedy = True, max_length = config.max_length)
                # |y_hat| = (batch_size, length, output_size)
                # |indice| = (batch_size, length)

                reward = get_reward(y, indice, n_gram = config.rl_n_gram)

                total_reward += float(reward.sum())
                sample_cnt += batch_size
                if sample_cnt >= len(valid_iter.dataset.examples):
                    break

            avg_bleu = total_reward / sample_cnt
            print("valid BLEU: %.4f" % avg_bleu)

            if highest_valid_bleu < avg_bleu:
                highest_valid_bleu = avg_bleu
                no_improve_cnt = 0
            else:
                no_improve_cnt += 1

            model.train()

        model_fn = config.model.split(".")
        model_fn = model_fn[:-1] + ["%02d" % (config.n_epochs + epoch), "%.2f-%.4f" % (train_bleu, avg_bleu)] + [model_fn[-1]]

        # PyTorch provides efficient method for save and load model, which uses python pickle.
        to_save = {"model": model.state_dict(),
                    "config": config,
                    "epoch": config.n_epochs + epoch + 1,
                    "current_lr": current_lr
                    }
        if others_to_save is not None:
            for k, v in others_to_save.items():
                to_save[k] = v
        torch.save(to_save, '.'.join(model_fn))

        if config.early_stop > 0 and no_improve_cnt > config.early_stop:
            break
