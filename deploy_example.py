# The following is an example chute deployment for the GLM-4.5-Air model from: https://chutes.ai/app/chute/7fa03c12-823f-529a-8245-36432f03e9a1?tab=source

import os
from chutes.chute import NodeSelector
from chutes.chute.template.sglang import build_sglang_chute

# speed up hf download
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

for key in ["NCCL_P2P_DISABLE", "NCCL_IB_DISABLE", "NCCL_NET_GDR_LEVEL"]:
    if key in os.environ:
        del os.environ[key]
if os.getenv("CHUTES_EXECUTION_CONTEXT") == "REMOTE":
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = os.path.join(os.getenv("HF_HOME", "/cache"), "glmairtic")

chute = build_sglang_chute(
    username="your_chutes_username",
    readme="zai-org/GLM-4.5-Air",
    model_name="zai-org/GLM-4.5-Air",
    image="chutes/sglang:nightly-2025110402",
    concurrency=40,
    revision="e7fdb9e0a52d2e0aefea94f5867c924a32a78d17",
    node_selector=NodeSelector(
        gpu_count=8,
        include=[
            "h100",
            "h100_sxm",
            "h800",
            "l40s",
            "a6000_ada",
            "a100",
            "a100_sxm",
            "a100_40gb",
            "a100_40gb_sxm",
            "h20",
        ],
    ),
    engine_args=(
        "--cuda-graph-max-bs 40 "
        "--tool-call-parser glm45 "
        "--reasoning-parser glm45"
    ),
)
