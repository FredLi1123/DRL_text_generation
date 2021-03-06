from torch.autograd import Variable
import torch.nn as nn
import argparse
import data
import model
import torch
from reinforce_reduced import Reinforce
from tqdm import tqdm
from torch import optim
import time

from utils import Recorder, annealing


def repackage_hidden(h):
    """Wraps hidden states in new Variables, to detach them from their history."""
    if type(h) == Variable:
        return Variable(h.data)
    else:
        return tuple(repackage_hidden(v) for v in h)


def get_batch(source, i, cfg):
    seq_len = min(cfg['max_len'], len(source) - 1 - i)
    data = Variable(source[i:i + seq_len], requires_grad=False).cuda()
    target = Variable(source[i + 1: i + 1 + seq_len], requires_grad=False).cuda()
    return data, target


def evaluate(data_source, model, cfg):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    ntokens = cfg['dict_size']

    criterion = nn.CrossEntropyLoss()
    for i in tqdm(range(0, data_source.size(0) - 1, cfg['max_len'])):
        data, targets = get_batch(data_source, i, cfg)
        hidden = model.init_hidden(cfg['batch_size'])
        output, _ = model(data, hidden)
        output_flat = output.view(-1, ntokens)
        total_loss += len(data) * criterion(output_flat, targets.view(-1)).data
        # hidden = repackage_hidden(hidden)
    model.train()
    return total_loss[0] / len(data_source)


def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    # data = data.cuda()
    return data


def save_model(path, model):
    with open(path, 'wb') as f:
        torch.save(model, f)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, default='./data/wikitext-2',
                        help='location of the data corpus')
    parser.add_argument('--output', type=str, default='./results/log.txt',
                        help='location of log file')
    parser.add_argument('--lr', type=float, default=0.001,
                        help='initial learning rate')
    parser.add_argument('--sigma', type=float, default=0.01,
                        help='standard deviation for policy')
    parser.add_argument('--gamma', type=float, default=1.0,
                        help='discount')
    parser.add_argument('--alpha', type=float, default=0.1,
                        help='ratio')
    parser.add_argument('--clip', type=float, default=0.25,
                        help='gradient clipping')
    parser.add_argument('--epochs', type=int, default=40,
                        help='upper epoch limit')
    parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                        help='batch size')
    parser.add_argument('--bptt', type=int, default=200,
                        help='sequence length')
    parser.add_argument('--seed', type=int, default=1111,
                        help='random seed')
    parser.add_argument('--gpu', action='store_true',
                        help='use GPU')
    parser.add_argument('--init', type=str,
                        default='baseline_model.pt', help="The LSTM model")
    parser.add_argument('--report', type=int,
                        default=50, help="The report interval")
    args = parser.parse_args()

    corpus = data.Corpus(args.data)
    cfg = dict()
    cfg['dict_size'] = len(corpus.dictionary)
    cfg['output_file'] = args.output
    cfg['init'] = args.init
    cfg['max_len'] = args.bptt
    cfg['epochs'] = args.epochs
    cfg['GPU'] = args.gpu
    cfg['lr'] = args.lr
    cfg['sigma'] = args.sigma
    cfg['gamma'] = args.gamma
    cfg['alpha'] = args.alpha
    cfg['batch_size'] = args.batch_size
    cfg['saveto'] = './model_200/'
    cfg['report_interval'] = args.report
    

    print(cfg)
    train_data = batchify(corpus.train, cfg['batch_size'])
    val_data = batchify(corpus.valid, cfg['batch_size'])
    test_data = batchify(corpus.test, cfg['batch_size'])

    with open(cfg['init'], 'rb') as f:
        policy = torch.load(f)
        print(policy)

    reinforce_model = Reinforce(policy=policy, sigma=cfg['sigma'], gamma=cfg['gamma'])
    recorder = Recorder(output_path=cfg['output_file'])

    valid_loss = []
    loss = evaluate(val_data, reinforce_model.policy, cfg)
    print('start from valid loss = ', loss)
    valid_loss.append(loss)

    ntokens = cfg['dict_size']

    optimizer = optim.Adam(reinforce_model.parameters(), lr=cfg['lr'])
    start_time = time.time()
    for epoch in range(cfg['epochs']):
        total_loss = 0.0
        total_LM_loss = 0.0

        for i in range(0, train_data.size(0) - 1, cfg['max_len']):
            optimizer.zero_grad()
            data, targets = get_batch(train_data, i, cfg)
            base_hidden = policy.init_hidden(bsz=cfg['batch_size'])
            hidden = policy.init_hidden(bsz=cfg['batch_size'])
            loss, LM_loss = reinforce_model(data, targets, hidden, base_hidden, cfg['alpha'])
            total_loss += loss.data
            total_LM_loss += LM_loss
            loss.backward()
            optimizer.step()
            
            # base_hidden = repackage_hidden(base_hidden)
            # hidden = repackage_hidden(hidden)
            nbsz = (i // cfg['max_len'] + 1)
            if nbsz % cfg['report_interval'] == 0:
                print('batch ', nbsz, ': loss = ', total_loss.cpu().numpy() / cfg['report_interval'])
                print('batch ', nbsz, ': LM loss = ', total_LM_loss / cfg['report_interval'])
                total_loss = 0.0
                total_LM_loss = 0.0
                # print('elapse time: ', time.time() - start_time)
                # loss = evaluate(val_data, reinforce_model.policy, cfg)
                # print('validation loss: ', loss)

        print('Epoch: ', epoch, ' elapse:', time.time() - start_time)
        loss = evaluate(val_data, reinforce_model.policy, cfg)
        if loss > valid_loss[-1]:
            annealing(optimizer, decay_rate=2)
            cfg['lr'] /= 2
            print('learning rate anneals to ', cfg['lr'])
        valid_loss.append(loss)
        recorder.record(epoch, cfg['alpha'], loss)
        save_path = cfg['saveto'] + '_epoch' + str(epoch) + '_loss' + str(loss)
        save_model(save_path, reinforce_model.policy)
        print('Epoch: ', epoch, ' save to ', save_path)

    test_loss = evaluate(test_data, reinforce_model.policy, cfg)
    recorder.record('', cfg['alpha'], test_loss)

    recorder.close()
