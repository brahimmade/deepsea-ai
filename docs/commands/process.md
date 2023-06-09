!!! note "About the process command"

    If you have just a few videos to process, or are experimenting, the **process** command is recommended.
    This creates a SageMaker Processing Job which uses the [SageMaker ScriptProcessor](https://docs.aws.amazon.com/sagemaker/latest/dg/processing-container-run-scripts.html).
    which processes videos one-by-one in sequence. This is the easiest way to get started, but it is not the most efficient or cost-effective way to process videos.
 
The SAGEMAKER_ROLE environment variable is used in the process command. This role is captured in the following order or precedence:
1. Through the SAGEMAKER_ROLE environment variable
2. Through the --config option in the [aws] section or when you run the ``deepsea-ai setup`` command

If you are switching AWS accounts, you will need to either set SAGEMAKER_ROLE  or run ``deepsea-ai setup`` again in that new account (recommended).

```
export SAGEMAKER_ROLE="arn:aws:iam::872338704006:role/DeepSeaAI"
```

## Process Examples

To run one or more videos in a directory with the job name *"DocRickets dive 1423"*  

```
deepsea-ai process -j "DocRickets dive 1423" -i /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/ 
```

or, to specify the tracker as either **strongsort** or **deepsort**

```
deepsea-ai process -j "DocRickets dive 1423" -i /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/ --tracker strongsort 
```

To use a different pretrained model, use the *model-s3* option e.g.

```
deepsea-ai process -j "DocRickets dive 1423" -i /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/ --tracker strongsort --model-s3 s3://902005-public/models/yolov5x_mbay_benthic_model.tar.gz
```

To specify the confidence and IOU thresholds, use the *conf-thres* and *iou-thres* options e.g.

```
deepsea-ai process -j "DocRickets dive 1423" -i /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/ --tracker strongsort --iou-thres 0.1 --conf-thres 0.5
```

To process a single video, e.g.

```
deepsea-ai process -j "DocRickets dive 1423" -i  /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/D1423_20220221T164250Z_h265.mp4 --tracker strongsort 
```

## ECS (Elastic Cluster Service) Process Examples

If you have setup an Elastic Cluster Service to process data in batch, you can use it with the **ecsprocess**
option. This is the most cost-effective way to process data in bulk.

To process videos in a directory with the job name *"DocRickets 2022/02"* in your cluster called *benthic33k* : 

```
deepsea-ai ecsprocess -u -c benthic33k -j "DocRickets dive 1423" -i /Volumes/M3/mezzanine/DocRicketts/2022/02/1423/ 
```

To process videos in a directory with the job name "DocRicketts 2021/08 with a cluster called mbari315k model", excluding any dives with the name D1371, D1374, or D1375

```
deepsea-ai ecsprocess -u \
        --job "DocRicketts 2021/08 with mbari315k model" \
        --cluster strongsort-yolov5-mbari315k  \
        --config 902005prod.ini \
        --conf-thres 0.2 \
        --iou-thres 0.2 \
        --input /Volumes/M3/mezzanine/DocRicketts/2021/08/ \
        --exclude D1371 --exclude D1374 --exclude D1375
```