from pathlib import Path
from metasmith import *

def protocol(context: ExecutionContext):
    Log.Info("this is diamond!")
    for k, x in context.outputs.items():
        context.Shell(f"touch {x.source}")
    return ExecutionResult(success=True)

# todo: url for more consistency
lib = DataTypeLibrary.Load("/home/tony/workspace/tools/Metasmith/main/local_mock/prototypes/metagenomics.dev3.yml")
model = Transform()
dep = model.AddRequirement(node=lib["oci_image_diamond"])
dep = model.AddRequirement(node=lib["orfs_faa"])
dep = model.AddRequirement(node=lib["protein_reference_diamond"])

TransformInstance(
    protocol = protocol,
    model = model,
    output_signature = {
        model.AddProduct(node=lib["orf_annotations"]): Path("annotations.csv"),
    },
)
