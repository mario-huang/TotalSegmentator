import os
import sys
import random
import string
import shutil
from pathlib import Path
from os.path import join
import numpy as np
import nibabel as nib

from totalsegmentator.libs import nostdout

with nostdout():
    from nnunet.inference.predict import predict_from_folder
    from nnunet.paths import default_plans_identifier, network_training_output_dir, default_trainer

from totalsegmentator.map_to_binary import class_map
from totalsegmentator.alignment import as_closest_canonical_nifti, undo_canonical_nifti
from totalsegmentator.resampling import change_spacing


def _get_full_task_name(task_id: int, src: str="raw"):
    if src == "raw":
        base = Path(os.environ['nnUNet_raw_data_base']) / "nnUNet_raw_data"
    elif src == "preprocessed":
        base = Path(os.environ['nnUNet_preprocessed'])
    elif src == "results":
        base = Path(os.environ['RESULTS_FOLDER']) / "nnUNet" / "3d_fullres"
    dirs = [str(dir).split("/")[-1] for dir in base.glob("*")]
    for dir in dirs:
        if f"Task{task_id:03d}" in dir:
            return dir

    # If not found in 3d_fullres, search in 2d
    if src == "results":
        base = Path(os.environ['RESULTS_FOLDER']) / "nnUNet" / "2d"
        dirs = [str(dir).split("/")[-1] for dir in base.glob("*")]
        for dir in dirs:
            if f"Task{task_id:03d}" in dir:
                return dir

    raise ValueError(f"task_id {task_id} not found")


def contains_empty_img(imgs):
    """
    imgs: List of image pathes
    """
    is_empty = True
    for img in imgs:
        this_is_empty = len(np.unique(nib.load(img).get_fdata())) == 1
        is_empty = is_empty and this_is_empty
    return is_empty


def nnUNet_predict(dir_in, dir_out, task_id, model="3d_fullres", folds=None,
                   trainer="nnUNetTrainerV2", tta=False):
    """
    Identical to bash function nnUNet_predict

    folds:  folds to use for prediction. Default is None which means that folds will be detected 
            automatically in the model output folder.
            for all folds: None
            for only fold 0: [0]
    """
    save_npz = False
    num_threads_preprocessing = 6
    num_threads_nifti_save = 2
    # num_threads_preprocessing = 1
    # num_threads_nifti_save = 1
    lowres_segmentations = None
    part_id = 0
    num_parts = 1
    disable_tta = not tta
    overwrite_existing = False
    # mode = "normal"
    mode = "fastest"
    all_in_gpu = None
    step_size = 0.5
    chk = "model_final_checkpoint"
    disable_mixed_precision = False

    task_id = int(task_id)
    task_name = _get_full_task_name(task_id, src="results")

    # trainer_class_name = default_trainer
    # trainer = trainer_class_name
    plans_identifier = default_plans_identifier

    model_folder_name = join(network_training_output_dir, model, task_name, trainer + "__" + plans_identifier)
    print("using model stored in ", model_folder_name)

    predict_from_folder(model_folder_name, dir_in, dir_out, folds, save_npz, num_threads_preprocessing,
                        num_threads_nifti_save, lowres_segmentations, part_id, num_parts, not disable_tta,
                        overwrite_existing=overwrite_existing, mode=mode, overwrite_all_in_gpu=all_in_gpu,
                        mixed_precision=not disable_mixed_precision,
                        step_size=step_size, checkpoint_name=chk)


def nnUNet_predict_image(file_in, file_out, task_id, model="3d_fullres", folds=None,
                         trainer="nnUNetTrainerV2", tta=False, multilabel_image=True, resample=None):
    """
    resample: None or float  (target spacing for all dimensions)
    """
    file_in, file_out = Path(file_in), Path(file_out)
    
    tmp_dir = file_in.parent / ("nnunet_tmp_" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8)))
    (tmp_dir).mkdir(exist_ok=True)

    # shutil.copy(file_in, tmp_dir / "s01_0000.nii.gz")
    as_closest_canonical_nifti(file_in, tmp_dir / "s01_0000.nii.gz")

    if resample is not None:
        print(f"Resampling to: {resample}")
        img_in = nib.load(tmp_dir / "s01_0000.nii.gz")
        img_in_shape = img_in.shape
        img_in_rsp = change_spacing(img_in, [resample, resample, resample], order=3, dtype=np.int32)
        nib.save(img_in_rsp, tmp_dir / "s01_0000.nii.gz")

    # with nostdout():
    nnUNet_predict(tmp_dir, tmp_dir, task_id, model, folds, trainer, tta)

    if resample is not None:
        print(f"Resampling to: {img_in_shape}")
        img_pred = nib.load(tmp_dir / "s01.nii.gz")
        img_pred_rsp = change_spacing(img_pred, [resample, resample, resample], img_in_shape, order=0, dtype=np.uint8)
        nib.save(img_pred_rsp, tmp_dir / "s01.nii.gz")

    undo_canonical_nifti(tmp_dir / "s01.nii.gz", tmp_dir / "s01_0000.nii.gz", tmp_dir / "s01.nii.gz")

    if multilabel_image:
        shutil.copy(tmp_dir / "s01.nii.gz", file_out)
    else:  # save each class as a separate binary image
        file_out.mkdir(exist_ok=True, parents=True)
        img = nib.load(tmp_dir / "s01.nii.gz")
        img_data = img.get_fdata()
        for k, v in class_map.items():
            binary_img = img_data == k
            nib.save(nib.Nifti1Image(binary_img.astype(np.uint8), img.affine, img.header), 
                    file_out / f"{v}.nii.gz")
            
    shutil.rmtree(tmp_dir)

    # todo: Add try except around everything and if fails, then remove nnunet_tmp dir
    #       Is there a smarter way to cleanup tmp files in error case?