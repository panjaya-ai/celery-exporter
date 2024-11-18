# celery-exporter ![Build Status](https://github.com/danihodovic/celery-exporter/actions/workflows/.github/workflows/ci.yml/badge.svg) [![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

1. Enable buildx run

```bash
docker buildx create --use
```
2. Do docker login to rellevant repository. For example for staging.

``````bash
 aws  ecr   get-login-password --region eu-west-1 --profile <profile> | docker login --username AWS --password-stdin <repo_name>
``````
3. Run docker buildx build

```bash 
docker buildx build --platform linux/amd64,linux/arm64 -t <repo_name>:<tag> --push .
```
4. Update image using helm chart in devops repository. By updating the image tag in values.yaml file.

