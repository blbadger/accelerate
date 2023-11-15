# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import pickle
import tempfile
import unittest
import warnings
from collections import UserDict, namedtuple
from unittest.mock import Mock, patch

import torch
from torch import nn
from transformers import AutoModelForCausalLM

from accelerate.state import PartialState
from accelerate.test_utils.testing import require_cuda, require_torch_min_version
from accelerate.test_utils.training import RegressionModel
from accelerate.utils import (
    check_os_kernel,
    convert_outputs_to_fp32,
    extract_model_from_parallel,
    find_device,
    get_state_dict_offloaded_model,
    listify,
    patch_environment,
    recursively_apply,
    save,
    send_to_device,
)


ExampleNamedTuple = namedtuple("ExampleNamedTuple", "a b c")


class UtilsTester(unittest.TestCase):
    def setUp(self):
        # logging requires initialized state
        PartialState()

    def test_send_to_device(self):
        tensor = torch.randn(5, 2)
        device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

        result1 = send_to_device(tensor, device)
        self.assertTrue(torch.equal(result1.cpu(), tensor))

        result2 = send_to_device((tensor, [tensor, tensor], 1), device)
        self.assertIsInstance(result2, tuple)
        self.assertTrue(torch.equal(result2[0].cpu(), tensor))
        self.assertIsInstance(result2[1], list)
        self.assertTrue(torch.equal(result2[1][0].cpu(), tensor))
        self.assertTrue(torch.equal(result2[1][1].cpu(), tensor))
        self.assertEqual(result2[2], 1)

        result2 = send_to_device({"a": tensor, "b": [tensor, tensor], "c": 1}, device)
        self.assertIsInstance(result2, dict)
        self.assertTrue(torch.equal(result2["a"].cpu(), tensor))
        self.assertIsInstance(result2["b"], list)
        self.assertTrue(torch.equal(result2["b"][0].cpu(), tensor))
        self.assertTrue(torch.equal(result2["b"][1].cpu(), tensor))
        self.assertEqual(result2["c"], 1)

        result3 = send_to_device(ExampleNamedTuple(a=tensor, b=[tensor, tensor], c=1), device)
        self.assertIsInstance(result3, ExampleNamedTuple)
        self.assertTrue(torch.equal(result3.a.cpu(), tensor))
        self.assertIsInstance(result3.b, list)
        self.assertTrue(torch.equal(result3.b[0].cpu(), tensor))
        self.assertTrue(torch.equal(result3.b[1].cpu(), tensor))
        self.assertEqual(result3.c, 1)

        result4 = send_to_device(UserDict({"a": tensor, "b": [tensor, tensor], "c": 1}), device)
        self.assertIsInstance(result4, UserDict)
        self.assertTrue(torch.equal(result4["a"].cpu(), tensor))
        self.assertIsInstance(result4["b"], list)
        self.assertTrue(torch.equal(result4["b"][0].cpu(), tensor))
        self.assertTrue(torch.equal(result4["b"][1].cpu(), tensor))
        self.assertEqual(result4["c"], 1)

    def test_honor_type(self):
        with self.assertRaises(TypeError) as cm:
            _ = recursively_apply(torch.tensor, (torch.tensor(1), 1), error_on_other_type=True)
        self.assertEqual(
            str(cm.exception),
            "Unsupported types (<class 'int'>) passed to `tensor`. Only nested list/tuple/dicts of objects that are valid for `is_torch_tensor` should be passed.",
        )

    def test_listify(self):
        tensor = torch.tensor([1, 2, 3, 4, 5])
        self.assertEqual(listify(tensor), [1, 2, 3, 4, 5])

        tensor = torch.tensor([[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]])
        self.assertEqual(listify(tensor), [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]])

        tensor = torch.tensor([[[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]], [[11, 12, 13, 14, 15], [16, 17, 18, 19, 20]]])
        self.assertEqual(
            listify(tensor), [[[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]], [[11, 12, 13, 14, 15], [16, 17, 18, 19, 20]]]
        )

    def test_patch_environment(self):
        with patch_environment(aa=1, BB=2):
            self.assertEqual(os.environ.get("AA"), "1")
            self.assertEqual(os.environ.get("BB"), "2")

        self.assertNotIn("AA", os.environ)
        self.assertNotIn("BB", os.environ)

    def test_patch_environment_key_exists(self):
        # check that patch_environment correctly restores pre-existing env vars
        with patch_environment(aa=1, BB=2):
            self.assertEqual(os.environ.get("AA"), "1")
            self.assertEqual(os.environ.get("BB"), "2")

            with patch_environment(Aa=10, bb="20", cC=30):
                self.assertEqual(os.environ.get("AA"), "10")
                self.assertEqual(os.environ.get("BB"), "20")
                self.assertEqual(os.environ.get("CC"), "30")

            self.assertEqual(os.environ.get("AA"), "1")
            self.assertEqual(os.environ.get("BB"), "2")
            self.assertNotIn("CC", os.environ)

        self.assertNotIn("AA", os.environ)
        self.assertNotIn("BB", os.environ)
        self.assertNotIn("CC", os.environ)

    def test_can_undo_convert_outputs(self):
        model = RegressionModel()
        model._original_forward = model.forward
        model.forward = convert_outputs_to_fp32(model.forward)
        model = extract_model_from_parallel(model, keep_fp32_wrapper=False)
        _ = pickle.dumps(model)

    @require_cuda
    def test_can_undo_fp16_conversion(self):
        model = RegressionModel()
        model._original_forward = model.forward
        model.forward = torch.cuda.amp.autocast(dtype=torch.float16)(model.forward)
        model.forward = convert_outputs_to_fp32(model.forward)
        model = extract_model_from_parallel(model, keep_fp32_wrapper=False)
        _ = pickle.dumps(model)

    @require_cuda
    @require_torch_min_version(version="2.0")
    def test_dynamo(self):
        model = RegressionModel()
        model._original_forward = model.forward
        model.forward = torch.cuda.amp.autocast(dtype=torch.float16)(model.forward)
        model.forward = convert_outputs_to_fp32(model.forward)
        model.forward = torch.compile(model.forward, backend="inductor")
        inputs = torch.randn(4, 10).cuda()
        _ = model(inputs)

    def test_extract_model(self):
        model = RegressionModel()
        # could also do a test with DistributedDataParallel, but difficult to run on CPU or single GPU
        distributed_model = torch.nn.parallel.DataParallel(model)
        model_unwrapped = extract_model_from_parallel(distributed_model)

        self.assertEqual(model, model_unwrapped)

    @require_torch_min_version(version="2.0")
    def test_dynamo_extract_model(self):
        model = RegressionModel()
        compiled_model = torch.compile(model)

        # could also do a test with DistributedDataParallel, but difficult to run on CPU or single GPU
        distributed_model = torch.nn.parallel.DataParallel(model)
        distributed_compiled_model = torch.compile(distributed_model)
        compiled_model_unwrapped = extract_model_from_parallel(distributed_compiled_model)

        self.assertEqual(compiled_model._orig_mod, compiled_model_unwrapped._orig_mod)

    def test_find_device(self):
        self.assertEqual(find_device([1, "a", torch.tensor([1, 2, 3])]), torch.device("cpu"))
        self.assertEqual(find_device({"a": 1, "b": torch.tensor([1, 2, 3])}), torch.device("cpu"))
        self.assertIsNone(find_device([1, "a"]))

    def test_check_os_kernel_no_warning_when_release_gt_min(self):
        # min version is 5.5
        with patch("platform.uname", return_value=Mock(release="5.15.0-35-generic", system="Linux")):
            with warnings.catch_warnings(record=True) as w:
                check_os_kernel()
            self.assertEqual(len(w), 0)

    def test_check_os_kernel_no_warning_when_not_linux(self):
        # system must be Linux
        with patch("platform.uname", return_value=Mock(release="5.4.0-35-generic", system="Darwin")):
            with warnings.catch_warnings(record=True) as w:
                check_os_kernel()
            self.assertEqual(len(w), 0)

    def test_check_os_kernel_warning_when_release_lt_min(self):
        # min version is 5.5
        with patch("platform.uname", return_value=Mock(release="5.4.0-35-generic", system="Linux")):
            with self.assertLogs() as ctx:
                check_os_kernel()
            self.assertEqual(len(ctx.records), 1)
            self.assertEqual(ctx.records[0].levelname, "WARNING")
            self.assertIn("5.4.0", ctx.records[0].msg)
            self.assertIn("5.5.0", ctx.records[0].msg)

    def test_save_safetensor_shared_memory(self):
        class Model(nn.Module):
            def __init__(self):
                super().__init__()
                self.a = nn.Linear(100, 100)
                self.b = self.a

            def forward(self, x):
                return self.b(self.a(x))

        model = Model()
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_path = os.path.join(tmp_dir, "model.safetensors")
            with self.assertLogs(level="WARNING") as log:
                save(model.state_dict(), save_path, safe_serialization=True)
                self.assertEqual(len(log.records), 1)
                self.assertIn("Removed shared tensor", log.output[0])

    @require_cuda
    def test_offload_save_state_dict(self):
        device_map = {
            "transformer.wte": 0,
            "transformer.wpe": 0,
            "transformer.h.0": "cpu",
            "transformer.h.1": "cpu",
            "transformer.h.2": "cpu",
            "transformer.h.3": "disk",
            "transformer.h.4": "disk",
            "transformer.ln_f": 0,
            "lm_head": 0,
        }
        model = AutoModelForCausalLM.from_pretrained("hf-internal-testing/tiny-random-gpt2", device_map=device_map)
        offload_state_dict = get_state_dict_offloaded_model(model)
        onloaded_model = AutoModelForCausalLM.from_pretrained(
            "hf-internal-testing/tiny-random-gpt2", device_map=device_map
        )
        onload_state_dict = onloaded_model.state_dict()
        # check equality in onloaded and offloaded state dicts
        for key in offload_state_dict:
            self.assertTrue(torch.all(offload_state_dict[key].to("cpu") == onload_state_dict[key].to("cpu")))

        # check that no key in the onloaded model's state dict is absent from the offloaded state dict
        for key in onload_state_dict:
            self.assertTrue(key in offload_state_dict)
