"""Validation."""
import logging
import time

import torch
import crypten
import crypten.nn as cnn

from utils.config import DEVICE_MODE, FLAGS, _ENV_EXPAND
from utils.common import set_random_seed
from utils.common import setup_logging
from utils.secure_common import get_device
from utils.common import bn_calibration
from utils import dataflow
from utils import secure_distributed as udist
from mmseg import seg_dataflow
from mmseg.loss import CrossEntropyLoss, JointsMSELoss, accuracy_keypoint
from utils.fix_hook import fix_deps

import secure_common as mc


# STOP THE PRESSES: Fix `cnn.Module`s not having some of the functions we expect (but would support)
fix_deps()

def run_one_epoch(epoch,
                  loader,
                  model,
                  criterion,
                  optimizer,
                  lr_scheduler,
                  ema,
                  meters,
                  max_iter=None,
                  phase='train'):
    """Run one epoch."""
    assert phase in ['val', 'test', 'bn_calibration'
                    ], "phase not be in val/test/bn_calibration."
    model.eval()
    if phase == 'bn_calibration':
        model.apply(bn_calibration)

    if FLAGS.use_distributed:
        loader.sampler.set_epoch(epoch)

    data_iterator = iter(loader)
    if FLAGS.use_distributed:
        data_fetcher = dataflow.DataPrefetcher(data_iterator)
    else:
        logging.warning('Not use prefetcher')
        data_fetcher = data_iterator
    for batch_idx, (input, target) in enumerate(data_fetcher):
        # used for bn calibration
        if max_iter is not None:
            assert phase == 'bn_calibration'
            if batch_idx >= max_iter:
                break

        if DEVICE_MODE == "gpu":
            target = target.cuda(non_blocking=True)
        # mc.forward_loss(model, criterion, input, target, meters)
        mc.forward_loss(model, criterion, input, target, meters, task=FLAGS.model_kwparams.task, distill=False)

    results = mc.reduce_and_flush_meters(meters)
    if udist.is_master():
        logging.info('Epoch {}/{} {}: '.format(epoch, FLAGS.num_epochs, phase)
                     + ', '.join(
                         '{}: {:.4f}'.format(k, v) for k, v in results.items()))
        for k, v in results.items():
            mc.summary_writer.add_scalar('{}/{}'.format(phase, k), v,
                                         FLAGS._global_step)
    return results


def val():
    """Validation."""
    torch.backends.cudnn.benchmark = True

    # model
    model, model_wrapper = mc.get_model()
    # for key, value in model_wrapper.state_dict().items():
    #     print(key, value.size())
    ema = mc.setup_ema(model)

    # Set Criterion
    criterion = torch.nn.CrossEntropyLoss(reduction='mean')
    if model.task == 'segmentation':
        criterion = CrossEntropyLoss()
    if FLAGS.dataset == 'coco':
        criterion = JointsMSELoss(use_target_weight=True)

    if DEVICE_MODE == "gpu": criterion = criterion.cuda()
    # distributed

    # check pretrained
    if FLAGS.pretrained:
        checkpoint = torch.load(FLAGS.pretrained,
                                map_location=lambda storage, loc: storage)
        if ema:
            checkpoint_ema = checkpoint['ema'].state_dict()
            filtered_info = {key: val for key, val in checkpoint_ema['info'].items() if 'bns' not in key}
            filtered_shadow = {key: val for key, val in checkpoint_ema['shadow'].items() if 'bns' not in key}
            checkpoint_ema['info'] = filtered_info
            checkpoint_ema['shadow'] = filtered_shadow

            ema.load_state_dict(checkpoint_ema)
            ema.to(get_device(model))
            # print("*******Test here:", ema.average_names())
        
        checkpoint_state_dict = {key: value for key, value in checkpoint['model'].state_dict().items() if 'bns' not in key}

        model_wrapper.load_state_dict(checkpoint_state_dict, strict=False)
        logging.info('Loaded model {}.'.format(FLAGS.pretrained))

    if udist.is_master():
        logging.info(model_wrapper)

    # data
    if FLAGS.dataset == 'cityscapes':
        (train_set, val_set, test_set) = seg_dataflow.cityscapes_datasets(FLAGS)
    elif FLAGS.dataset == 'ade20k':
        (train_set, val_set, test_set) = seg_dataflow.ade20k_datasets(FLAGS)
    elif FLAGS.dataset == 'coco':
        (train_set, val_set, test_set) = seg_dataflow.coco_datasets(FLAGS)
        # print(len(train_set), len(val_set))  # 149813 104125
    else:
        # data
        (train_transforms, val_transforms,
         test_transforms) = dataflow.data_transforms(FLAGS)
        (train_set, val_set, test_set) = dataflow.dataset(train_transforms,
                                                          val_transforms,
                                                          test_transforms, FLAGS)
        
    _, calib_loader, _, test_loader = dataflow.data_loader(
        train_set, val_set, test_set, FLAGS)

    if udist.is_master():
        logging.info('Start testing.')
    FLAGS._global_step = 0

    # Get test meters
    if not FLAGS.distill:
        test_meters = mc.get_meters('test')
    else:
        test_meters = mc.get_distill_meters('test')
    if FLAGS.model_kwparams.task == 'segmentation':
        if not FLAGS.distill:
            test_meters = mc.get_seg_meters('test')
        else:
            test_meters = mc.get_seg_distill_meters('test')

    validate(0, calib_loader, test_loader, criterion, test_meters,
             model_wrapper, ema, 'test')
    return


def validate(epoch, calib_loader, val_loader, criterion, val_meters,
             model_wrapper, ema, phase):
    """Calibrate and validate."""
    assert phase in ['test', 'val']
    model_eval_wrapper = mc.get_ema_model(ema, model_wrapper)

    # bn_calibration
    if FLAGS.get('bn_calibration', False):
        if not FLAGS.use_distributed:
            logging.warning(
                'Only GPU0 is used when calibration when use DataParallel')
        with torch.no_grad():
            with crypten.no_grad():
                _ = run_one_epoch(epoch,
                                calib_loader,
                                model_eval_wrapper,
                                criterion,
                                None,
                                None,
                                None,
                                val_meters,
                                max_iter=FLAGS.bn_calibration_steps,
                                phase='bn_calibration')
        if FLAGS.use_distributed:
            udist.allreduce_bn(model_eval_wrapper)

    # val
    with torch.no_grad():
        with crypten.no_grad():
            results = run_one_epoch(epoch,
                                    val_loader,
                                    model_eval_wrapper,
                                    criterion,
                                    None,
                                    None,
                                    None,
                                    val_meters,
                                    phase=phase)
    return results


def main():
    """Entry."""
    FLAGS.test_only = True
    mc.setup_distributed()
    if udist.is_master():
        FLAGS.log_dir = '{}/{}'.format(FLAGS.log_dir,
                                       time.strftime("%Y%m%d-%H%M%S-eval"))
        setup_logging(FLAGS.log_dir)
        for k, v in _ENV_EXPAND.items():
            logging.info('Env var expand: {} to {}'.format(k, v))
        logging.info(FLAGS)

    set_random_seed(FLAGS.get('random_seed', 0))
    with mc.SummaryWriterManager():
        val()


if __name__ == "__main__":
    main()