# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import glob
import os
import re

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Get new version.
with open(os.path.join(root, "numpyro", "version.py")) as f:
    for line in f:
        if line.startswith("__version__ ="):
            new_version = line.strip().split()[-1]

# Collect potential files.
filenames = []
for path in ["examples", "notebooks/source"]:
    for ext in ["*.py", "*.ipynb"]:
        filenames.extend(glob.glob(os.path.join(root, path, "**", ext),
                                   recursive=True))
filenames.sort()

# Update version string.
pattern1 = re.compile("assert numpyro.__version__.startswith\\(\"[^\"]*\"\\)")
pattern2 = re.compile("assert numpyro.__version__.startswith\\('[^']*'\\)")
text = f"assert numpyro.__version__.startswith({new_version})"
for filename in filenames:
    with open(filename) as f:
        old_text = f.read()
    new_text = pattern1.sub(text, old_text)
    new_text = pattern2.sub(text, new_text)
    if new_text != old_text:
        print("updating {}".format(filename))
    with open(filename, "w") as f:
        f.write(new_text)
