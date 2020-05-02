import torch
from torch import nn
from torch.nn import functional as F

from detectron2.config import configurable
from detectron2.layers import Linear, ShapeSpec, batched_nms, cat
from detectron2.modeling.roi_heads import FastRCNNOutputLayers
from detectron2.modeling.roi_heads.fast_rcnn import fast_rcnn_inference
from detectron2.modeling.roi_heads.fast_rcnn import FastRCNNOutputs
from detectron2.utils.events import get_event_storage
from detectron2.data.catalog import MetadataCatalog
from detectron2.structures import Boxes, Instances


def interaction_inference_single_image(
    image_shape,
    person_boxes,
    object_boxes,
    person_box_scores,
    object_box_scores,
    object_box_classes,
    hoi_scores,
    score_thresh,
    topk_per_image
):
    """
    Single-image HOI inference.
    Return HOI detection results by thresholding on scores.

    Args:
        image_shape (tuple): (width, height) tuple for each image in the batch.
        person_boxes (Boxes): A `Boxes` has shape (N, 4), where N is the number of person boxes.
        object_boxes (Boxes): A `Boxes` has shape (M, 4), where M is the number of object boxes.
        person_box_scores (Tensor): A Tensor of predicted pesron box scores with shape (N, ).
        object_box_scores (Tensor): A Tensor of predicted object box scores with shape (M, ).
        object_box_classes (Tensor): A Tensor of predicted object box classes with shape (M, 1).
        hoi_scores (Tensor): A Tensor has shape (N, M, K), where K is the number of actions.
        score_thresh (float): Only return detections with a confidence score exceeding this
            threshold.
        topk_per_image (int): The number of top scoring detections to return. Set < 0 to return
            all detections.
    
    Returns:
        instances: (Instances): An `Instances` that stores the topk most confidence detections.
    """
    # Reweight interaction scores with (person & object) box scores
    box_scores = person_box_scores * object_box_scores
    scores = hoi_scores *  box_scores[:, None].repeat(1, hoi_scores.size(-1))

    # Filter results based on detection scores
    filter_mask = scores > score_thresh # (N, M, K)
    # (R, 2. First column contains indices coresponding to predictions.
    # Second column contains indices of classes.
    filter_inds = filter_mask.nonzero()

    person_boxes = person_boxes[filter_inds[:, 0]]
    object_boxes = object_boxes[filter_inds[:, 0]]
    object_classes = object_box_classes[filter_inds[:, 0]]
    action_classes = filter_inds[:, 1]
    scores = scores[filter_mask]

    if topk_per_image > 0 and topk_per_image < len(filter_inds):
        keep = torch.argsort(scores, descending=True)
        keep = keep[:topk_per_image]
        person_boxes, object_boxes = person_boxes[keep], object_boxes[keep]
        object_classes, action_classes = object_classes[keep], action_classes[keep]
        scores = scores[keep]

    result = Instances(image_shape)
    result.person_boxes = person_boxes
    result.object_boxes = object_boxes
    result.object_classes = object_classes
    result.action_classes = action_classes
    result.scores = scores
    return result


class BoxOutputs(FastRCNNOutputs):
    """
    A class that stores information about outputs of a Fast R-CNN head.
    It provides methods that are used to decode the outputs of a Fast R-CNN head.
    """

    def box_inference(self, score_thresh, nms_thresh, topk_per_image):  
        """
        Args:
            score_thresh (float): same as fast_rcnn_inference.
            nms_thresh (float): same as fast_rcnn_inference.
            topk_per_image (int): same as fast_rcnn_inference.
        Returns:
            list[Instances]: same as fast_rcnn_inference.
            list[Tensor]: same as fast_rcnn_inference.
        """
        boxes = self.predict_boxes()
        scores = self.pred_class_logits.split(self.num_preds_per_image, dim=0)
        image_shapes = self.image_shapes

        return fast_rcnn_inference(
            boxes, scores, image_shapes, score_thresh, nms_thresh, topk_per_image
        )

    def softmax_cross_entropy_loss(self):
        """
        Compute the softmax cross entropy loss for box classification.

        Returns:
            scalar Tensor
        """
        if self._no_instances:
            return 0.0 * F.cross_entropy(
                self.pred_class_logits,
                torch.zeros(0, dtype=torch.long, device=self.pred_class_logits.device),
                reduction="sum",
            )
        else:
            self._log_accuracy()
            # See ``:class:StandardHOROIHeads._forward_box``. Note that we have computed the
            # softmax of box scores at ``_reweight_box_given_proposal_scores``. Thus, here we
            # apply F.nll_loss() instead of F.cross_entropy()
            return F.nll_loss(torch.log(self.pred_class_logits), self.gt_classes, reduction="mean")

    def losses(self):
        """
        Compute the default losses for box head in Fast(er) R-CNN,
        with softmax cross entropy loss and smooth L1 loss.

        Returns:
            A dict of losses (scalar tensors) containing keys "loss_cls" and "loss_box_reg".
        """
        return {
            "loss_cls": self.softmax_cross_entropy_loss(),
            "loss_box_reg": self.smooth_l1_loss(),
        }


class HoiOutputs(object):
    """
    A class that stores information about outputs of a HOI head.
    """
    def __init__(self, pred_class_logits, hopairs, pos_weights):
        """
        Args:
            pred_class_logits (Tensor): A tensor of shape (R, K) storing the predicted
                action class logits for all R human-object pair instances.
                Each row corresponds to a human-object pair in "hopairs".
            hopairs (list[Instances]): A list of N Instances, where Instances i stores the
                proposal pairs for image i. When training, each Instances must have
                ground-truth labels stored in the field "gt_actions" and "gt_classes".
                The total number of all instances must be equal to R.
        """
        self.device = pred_class_logits.device
        self.num_preds_per_image = [len(p) for p in hopairs]
        self.pred_class_logits = pred_class_logits
        self.pos_weights = pos_weights.to(self.device)
        self.image_shapes = [x.image_size for x in hopairs]
        
        if len(hopairs):
            if hopairs[0].has("gt_actions"):
                # The following fields should exist only when training.
                self.gt_actions = cat([x.gt_actions for x in hopairs], dim=0)
                self.gt_actions = self.gt_actions.to(self.device)
            else:
                # The following fields should be available when inference.
                self.person_boxes = [x.person_boxes for x in hopairs]
                self.object_boxes = [x.object_boxes for x in hopairs]
                self.person_box_scores = [x.person_box_scores for x in hopairs]
                self.object_box_scores = [x.object_box_scores for x in hopairs]
                self.object_box_classes = [x.object_box_classes for x in hopairs]

        self._no_instances = len(hopairs) == 0  # no instances found

    def _log_accuracy(self):
        """
        Log the accuracy metrics to EventStorage.
        """
        gt_actions = self.gt_actions.flatten()
        num_instances = gt_actions.numel()

        pred_classes = torch.sigmoid(self.pred_class_logits).flatten()

        fg_inds = (gt_actions > 0)
        num_fg = fg_inds.nonzero().numel()
        fg_pred_classes = pred_classes[fg_inds]

        num_false_negative = (fg_pred_classes <= 0.5).nonzero().numel()
        num_accurate = ((pred_classes > 0.5) == gt_actions).nonzero().numel()
        fg_num_accurate = (fg_pred_classes > 0.5).nonzero().numel()

        storage = get_event_storage()
        if num_instances > 0:
            storage.put_scalar("action/cls_accuracy", num_accurate / num_instances)
            if num_fg > 0:
                storage.put_scalar("action/fg_cls_accuracy", fg_num_accurate / num_fg)
                storage.put_scalar("action/false_negative", num_false_negative / num_fg)

    def binary_cross_entropy_with_logits(self):
        """
        Compute the binary cross entropy loss for action classification.

        Returns:
            scalar Tensor
        """
        if self._no_instances:
            return 0.0 * F.binary_cross_entropy_with_logits(
                self.pred_class_logits,
                torch.zeros(self.pred_class_logits.size(), device=self.device),
                reduction="sum",
            )
        else:
            self._log_accuracy()
            return F.binary_cross_entropy_with_logits(
                self.pred_class_logits,
                self.gt_actions,
                reduction="mean",
                pos_weight=self.pos_weights
            )

    def losses(self):
        """
        Compute the default losses for action classification in hoi head.

        Returns:
            A dict of losses (scalar tensors) containing keys "loss_action".
        """
        return {
            "loss_action": self.binary_cross_entropy_with_logits(),
        }
    
    def predict_probs(self):
        """
        Returns:
            list[Tensor]: A list of Tensors of predicted class probabilities for each image.
                Element i has shape (Ri, K), where Ri is the number of human-object pairs
                for image i.
        """
        probs = torch.sigmoid(self.pred_class_logits)
        return probs.split(self.num_preds_per_image, dim=0)
        

    def inference(self, score_thresh, topk_per_image):
        """
        Args:
            score_thresh (float): Only return detections with a confidence score exceeding this
                threshold.
            topk_per_image (int): The number of top scoring detections to return. Set < 0 to return
                all detections.
        Returns:
            instances: (list[Instances]): A list of N instances, one for each image in the batch,
                that stores the topk most confidence detections.
        """
        hoi_scores = self.predict_probs()

        instances = []

        for image_id in range(len(self.image_shapes)):
            instances_per_image = interaction_inference_single_image(
                self.image_shapes[image_id],
                self.person_boxes[image_id],
                self.object_boxes[image_id],
                self.person_box_scores[image_id],
                self.object_box_scores[image_id],
                self.object_box_classes[image_id],
                hoi_scores[image_id],
                score_thresh,
                topk_per_image
            )
            instances.append(instances_per_image)

        return instances


class BoxOutputLayers(FastRCNNOutputLayers):
    """
    Two linear layers for predicting Fast R-CNN outputs:
      (1) proposal-to-detection box regression deltas
      (2) classification scores
    """

    def inference(self, predictions, proposals):
        scores, proposal_deltas = predictions
        return BoxOutputs(
            self.box2box_transform, scores, proposal_deltas, proposals, self.smooth_l1_beta
        ).box_inference(self.test_score_thresh, self.test_nms_thresh, self.test_topk_per_image)

    def losses(self, predictions, proposals):
        """
        Args:
            predictions: return values of :meth:`forward()`.
            proposals (list[Instances]): proposals that match the features
                that were used to compute predictions.
        """
        scores, proposal_deltas = predictions
        return BoxOutputs(
            self.box2box_transform, scores, proposal_deltas, proposals, self.smooth_l1_beta
        ).losses()


class HoiOutputLayers(nn.Module):
    """
    Two linear layers for predicting action classification scores for HOI.
    """
    @configurable
    def __init__(
        self,
        input_shape,
        num_classes,
        pos_weights,
        test_score_thresh=0.0,
        test_topk_per_image=100,
    ):
        """
        Args:
            input_shape (ShapeSpec): shape of the input feature to this module
            num_classes (int): number of action classes
            test_score_thresh (float): threshold to filter predictions results.
            test_topk_per_image (int): number of top predictions to produce per image.
        """
        super().__init__()
        if isinstance(input_shape, int):  # some backward compatbility
            input_shape = ShapeSpec(channels=input_shape)
        input_size = input_shape.channels * (input_shape.width or 1) * (input_shape.height or 1)
        # The prediction layer for num_classes foreground classes. The input should be
        # features from person, object and union region. Thus, the input size * 3.
        self.cls_fc1 = Linear(input_size * 3, input_size)
        self.cls_score = Linear(input_size, num_classes)

        for layer in [self.cls_fc1, self.cls_score]:
            nn.init.normal_(layer.weight, std=0.01)
            nn.init.constant_(layer.bias, 0)

        self.test_score_thresh = test_score_thresh
        self.test_topk_per_image = test_topk_per_image
        self.pos_weights = pos_weights

    @classmethod
    def from_config(cls, cfg, input_shape):
        # fmt: on
        num_classes          = cfg.MODEL.ROI_HEADS.NUM_ACTIONS
        test_score_thresh    = cfg.MODEL.ROI_HEADS.HOI_SCORE_THRESH_TEST
        test_topk_per_image  = cfg.TEST.INTERACTIONS_PER_IMAGE
        action_cls_weights   = cfg.MODEL.HOI_BOX_HEAD.ACTION_CLS_WEIGHTS
        batch_size_per_image = cfg.MODEL.ROI_HEADS.HOI_BATCH_SIZE_PER_IMAGE
        ims_per_batch        = cfg.SOLVER.IMS_PER_BATCH
        # fmt: off

        # Positive weights are used to balance the instances at training.
        # Get the prior distribution from the metadata.
        pos_weights = torch.full((num_classes, ), 0.)
        for dataset in cfg.DATASETS.TRAIN:
            meta = MetadataCatalog.get(dataset)
            priors = meta.get("action_priors", None)
            if priors:
                priors = torch.as_tensor(priors) * ims_per_batch * batch_size_per_image
                pos_weights_per_dataset = torch.clamp(
                    1./priors,
                    min=action_cls_weights[0],
                    max=action_cls_weights[1],
                )
                pos_weights += pos_weights_per_dataset
            else:
                pos_weights += torch.full((num_classes, ), 1.)

        if len(cfg.DATASETS.TRAIN):
            pos_weights /= len(cfg.DATASETS.TRAIN)

        return {
            "input_shape": input_shape,
            "num_classes": num_classes,
            "pos_weights": pos_weights,
            "test_score_thresh": test_score_thresh,
            "test_topk_per_image": test_topk_per_image,
        }

    def forward(self, u_x, p_x, o_x):
        """
        Returns:
            Tensor: NxK scores for each human-object pair
        """
        x = torch.cat([u_x, p_x, o_x], dim=-1)
        x = F.relu(self.cls_fc1(x))
        x = self.cls_score(x)
        return x

    def losses(self, pred_class_logits, hopairs):
        """
        Args:
            pred_class_logits: return values of :meth:`forward()`.
        """
        return HoiOutputs(pred_class_logits, hopairs, self.pos_weights).losses()

    def inference(self, pred_class_logits, hopairs):
        return HoiOutputs(pred_class_logits, hopairs, self.pos_weights).inference(
            self.test_score_thresh, self.test_topk_per_image
        )