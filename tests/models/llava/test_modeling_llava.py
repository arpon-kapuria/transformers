# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
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
"""Testing suite for the PyTorch Llava model."""

import copy
import unittest

import requests
from parameterized import parameterized

from transformers import (
    AutoProcessor,
    AutoTokenizer,
    LlavaConfig,
    LlavaForConditionalGeneration,
    LlavaModel,
    is_torch_available,
    is_vision_available,
)
from transformers.testing_utils import (
    Expectations,
    cleanup,
    require_bitsandbytes,
    require_torch,
    require_vision,
    slow,
    torch_device,
)

from ...generation.test_utils import GenerationTesterMixin
from ...test_configuration_common import ConfigTester
from ...test_modeling_common import ModelTesterMixin, floats_tensor, ids_tensor


if is_torch_available():
    import torch


if is_vision_available():
    from PIL import Image


class LlavaVisionText2TextModelTester:
    def __init__(
        self,
        parent,
        ignore_index=-100,
        image_token_index=0,
        projector_hidden_act="gelu",
        seq_length=7,
        vision_feature_select_strategy="default",
        vision_feature_layer=-1,
        text_config={
            "model_type": "llama",
            "seq_length": 7,
            "is_training": True,
            "use_input_mask": True,
            "use_token_type_ids": False,
            "use_labels": True,
            "vocab_size": 99,
            "hidden_size": 32,
            "num_hidden_layers": 2,
            "num_attention_heads": 4,
            "intermediate_size": 37,
            "hidden_act": "gelu",
            "hidden_dropout_prob": 0.1,
            "attention_probs_dropout_prob": 0.1,
            "max_position_embeddings": 512,
            "type_vocab_size": 16,
            "type_sequence_label_size": 2,
            "initializer_range": 0.02,
            "num_labels": 3,
            "num_choices": 4,
            "pad_token_id": 1,
        },
        is_training=True,
        vision_config={
            "image_size": 8,
            "patch_size": 2,
            "num_channels": 3,
            "is_training": True,
            "hidden_size": 32,
            "projection_dim": 32,
            "num_hidden_layers": 2,
            "num_attention_heads": 4,
            "intermediate_size": 37,
            "dropout": 0.1,
            "attention_dropout": 0.1,
            "initializer_range": 0.02,
        },
    ):
        self.parent = parent
        self.ignore_index = ignore_index
        self.image_token_index = image_token_index
        self.projector_hidden_act = projector_hidden_act
        self.vision_feature_select_strategy = vision_feature_select_strategy
        self.vision_feature_layer = vision_feature_layer
        self.text_config = text_config
        self.vision_config = vision_config
        self.pad_token_id = text_config["pad_token_id"]

        self.num_hidden_layers = text_config["num_hidden_layers"]
        self.vocab_size = text_config["vocab_size"]
        self.hidden_size = text_config["hidden_size"]
        self.num_attention_heads = text_config["num_attention_heads"]
        self.is_training = is_training

        self.batch_size = 3
        self.num_channels = 3
        self.image_size = 336
        self.num_image_tokens = (self.vision_config["image_size"] // self.vision_config["patch_size"]) ** 2
        self.seq_length = seq_length + self.num_image_tokens
        self.encoder_seq_length = self.seq_length

    def get_config(self):
        return LlavaConfig(
            text_config=self.text_config,
            vision_config=self.vision_config,
            ignore_index=self.ignore_index,
            image_token_index=self.image_token_index,
            projector_hidden_act=self.projector_hidden_act,
            vision_feature_select_strategy=self.vision_feature_select_strategy,
            vision_feature_layer=self.vision_feature_layer,
            image_seq_length=self.num_image_tokens,
        )

    def prepare_config_and_inputs(self):
        pixel_values = floats_tensor(
            [
                self.batch_size,
                self.vision_config["num_channels"],
                self.vision_config["image_size"],
                self.vision_config["image_size"],
            ]
        )
        config = self.get_config()

        return config, pixel_values

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        config, pixel_values = config_and_inputs
        input_ids = ids_tensor([self.batch_size, self.seq_length], config.text_config.vocab_size - 1) + 1
        input_ids[input_ids == config.image_token_index] = self.pad_token_id
        input_ids[:, : self.num_image_tokens] = config.image_token_index
        attention_mask = input_ids.ne(1).to(torch_device)

        inputs_dict = {
            "pixel_values": pixel_values,
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        return config, inputs_dict


@require_torch
class LlavaForConditionalGenerationModelTest(ModelTesterMixin, GenerationTesterMixin, unittest.TestCase):
    """
    Model tester for `LlavaForConditionalGeneration`.
    """

    all_model_classes = (
        (
            LlavaModel,
            LlavaForConditionalGeneration,
        )
        if is_torch_available()
        else ()
    )
    pipeline_model_mapping = (
        {"image-to-text": LlavaForConditionalGeneration, "image-text-to-text": LlavaForConditionalGeneration}
        if is_torch_available()
        else {}
    )
    test_pruning = False
    test_head_masking = False
    _is_composite = True

    def setUp(self):
        self.model_tester = LlavaVisionText2TextModelTester(self)
        common_properties = ["image_token_index", "vision_feature_layer", "image_seq_length"]
        self.config_tester = ConfigTester(
            self, config_class=LlavaConfig, has_text_modality=False, common_properties=common_properties
        )

    def test_config(self):
        self.config_tester.run_common_tests()

    def test_mismatching_num_image_tokens(self):
        """
        Tests that VLMs through an error with explicit message saying what is wrong
        when number of images doesn't match number of image tokens in the text.
        Also we need to test multi-image cases when one prompr has multiple image tokens.
        """
        config, input_dict = self.model_tester.prepare_config_and_inputs_for_common()
        for model_class in self.all_model_classes:
            model = model_class(config).to(torch_device)
            curr_input_dict = copy.deepcopy(input_dict)  # in=place modifications further
            _ = model(**curr_input_dict)  # successful forward with no modifications

            # remove one image but leave the image token in text
            curr_input_dict["pixel_values"] = curr_input_dict["pixel_values"][-1:, ...]
            with self.assertRaises(ValueError):
                _ = model(**curr_input_dict)

            # simulate multi-image case by concatenating inputs where each has exactly one image/image-token
            input_ids = curr_input_dict["input_ids"][:1]
            pixel_values = curr_input_dict["pixel_values"][:1]
            input_ids = torch.cat([input_ids, input_ids], dim=0)

            # one image and two image tokens raise an error
            with self.assertRaises(ValueError):
                _ = model(input_ids=input_ids, pixel_values=pixel_values)

            # two images and two image tokens don't raise an error
            pixel_values = torch.cat([pixel_values, pixel_values], dim=0)
            _ = model(input_ids=input_ids, pixel_values=pixel_values)

    @parameterized.expand(
        [
            (-1,),
            ([-1],),
            ([-1, -2],),
        ],
    )
    def test_vision_feature_layers(self, vision_feature_layer):
        """
        Test that we can use either one vision feature layer, or a list of
        vision feature layers.
        """
        config, input_dict = self.model_tester.prepare_config_and_inputs_for_common()
        config.vision_feature_layer = vision_feature_layer

        num_feature_layers = 1 if isinstance(vision_feature_layer, int) else len(vision_feature_layer)
        hidden_size = config.vision_config.hidden_size
        expected_features = hidden_size * num_feature_layers

        for model_class in self.all_model_classes:
            model = model_class(config).to(torch_device)
            # We should have the right number of input features,
            # and should be able to run a forward pass without exploding
            base_model = getattr(model, "model", model)
            assert base_model.multi_modal_projector.linear_1.in_features == expected_features
            model(**input_dict)

    @unittest.skip(
        reason="This architecture seems to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing(self):
        pass

    @unittest.skip(
        reason="This architecture seems to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant(self):
        pass

    @unittest.skip(
        reason="This architecture seems to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant_false(self):
        pass

    @unittest.skip(
        "VLMs need lots of steps to prepare images/mask correctly to get pad-free inputs. Can be tested as part of LLM test"
    )
    def test_flash_attention_2_padding_matches_padding_free_with_position_ids(self):
        pass


@require_torch
class LlavaForConditionalGenerationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.processor = AutoProcessor.from_pretrained("llava-hf/bakLlava-v1-hf")

    def tearDown(self):
        cleanup(torch_device, gc_collect=True)

    @slow
    @require_bitsandbytes
    def test_small_model_integration_test(self):
        # Let's make sure we test the preprocessing to replace what is used
        model = LlavaForConditionalGeneration.from_pretrained("llava-hf/bakLlava-v1-hf", load_in_4bit=True)

        prompt = "<image>\nUSER: What are the things I should be cautious about when I visit this place?\nASSISTANT:"
        image_file = "https://llava-vl.github.io/static/images/view.jpg"
        raw_image = Image.open(requests.get(image_file, stream=True).raw)
        inputs = self.processor(images=raw_image, text=prompt, return_tensors="pt").to(torch_device)

        output = model.generate(**inputs, max_new_tokens=20)
        expected_decoded_texts = Expectations({
            ("cuda", None): "\nUSER: What are the things I should be cautious about when I visit this place?\nASSISTANT: When visiting this place, there are a few things one should be cautious about. Firstly,",
            ("rocm", (9, 5)): "\nUSER: What are the things I should be cautious about when I visit this place?\nASSISTANT: When visiting this place, there are a few things one should be cautious about. First, the",
        })  # fmt: skip
        EXPECTED_DECODED_TEXT = expected_decoded_texts.get_expectation()

        self.assertEqual(
            self.processor.decode(output[0], skip_special_tokens=True),
            EXPECTED_DECODED_TEXT,
        )

    @slow
    @require_bitsandbytes
    def test_small_model_integration_test_llama_single(self):
        # Let's make sure we test the preprocessing to replace what is used
        model_id = "llava-hf/llava-1.5-7b-hf"

        model = LlavaForConditionalGeneration.from_pretrained("llava-hf/llava-1.5-7b-hf", load_in_4bit=True)
        processor = AutoProcessor.from_pretrained(model_id)

        prompt = "USER: <image>\nWhat are the things I should be cautious about when I visit this place? ASSISTANT:"
        image_file = "https://llava-vl.github.io/static/images/view.jpg"
        raw_image = Image.open(requests.get(image_file, stream=True).raw)
        inputs = processor(images=raw_image, text=prompt, return_tensors="pt").to(torch_device, torch.float16)

        output = model.generate(**inputs, max_new_tokens=900, do_sample=False)

        EXPECTED_DECODED_TEXTS = Expectations(
            {
                ("cuda", 7): 'USER:  \nWhat are the things I should be cautious about when I visit this place? ASSISTANT: When visiting this place, which is a pier or dock extending over a body of water, there are a few things to be cautious about. First, be aware of the weather conditions, as sudden changes in weather can make the pier unsafe to walk on. Second, be mindful of the water depth and any potential hazards, such as submerged rocks or debris, that could cause accidents or injuries. Additionally, be cautious of the tides and currents, as they can change rapidly and pose a risk to swimmers or those who venture too close to the edge of the pier. Lastly, be respectful of the environment and other visitors, as the pier is a shared space where people can enjoy the view, relax, or engage in recreational activities.',
                ("cuda", 8): 'USER:  \nWhat are the things I should be cautious about when I visit this place? ASSISTANT: When visiting this place, which is a pier or dock extending over a body of water, there are a few things to be cautious about. First, be aware of the weather conditions, as sudden changes in weather can make the pier unsafe to walk on. Second, be mindful of the water depth and any potential hazards, such as submerged rocks or debris, that could cause accidents or injuries. Additionally, be cautious of the tides and currents, as they can change rapidly and pose a risk to swimmers or those who venture too close to the edge of the pier. Lastly, be respectful of the environment and other visitors, as the pier is a shared space where people can enjoy the view, relax, or engage in recreational activities.',
                ("rocm", (9, 5)): 'USER:  \nWhat are the things I should be cautious about when I visit this place? ASSISTANT: When visiting this place, which is a pier or dock overlooking a lake, you should be cautious about the following:\n\n1. Safety: Ensure that the pier or dock is stable and secure before stepping onto it. Avoid walking on the edge of the pier or dock, as it could be unstable or unsafe.\n\n2. Weather conditions: Be aware of the weather forecast before visiting the area. Strong winds, heavy rain, or storms can make the pier or dock unsafe to use.\n\n3. Wildlife: Be mindful of the wildlife in the area, such as birds or aquatic animals. Avoid disturbing their natural habitat or causing harm to the local ecosystem.\n\n4. Water safety: If you plan to go swimming or engage in water activities, be aware of the water conditions, such as currents, tides, or potential hazards like submerged objects.\n\n5. Personal belongings: Keep an eye on your personal belongings, such as bags or backpacks, to prevent theft or loss.\n\n6. Leave no trace: When visiting the area, make sure to clean up after yourself and leave no trace of your presence to preserve the natural environment.',
            }
        )  # fmt: skip
        EXPECTED_DECODED_TEXT = EXPECTED_DECODED_TEXTS.get_expectation()

        self.assertEqual(
            processor.decode(output[0], skip_special_tokens=True),
            EXPECTED_DECODED_TEXT,
        )

    @slow
    @require_bitsandbytes
    def test_small_model_integration_test_llama_batched(self):
        # Let's make sure we test the preprocessing to replace what is used
        model_id = "llava-hf/llava-1.5-7b-hf"

        model = LlavaForConditionalGeneration.from_pretrained("llava-hf/llava-1.5-7b-hf", load_in_4bit=True)
        processor = AutoProcessor.from_pretrained(model_id)

        prompts = [
            "USER: <image>\nWhat are the things I should be cautious about when I visit this place? What should I bring with me? ASSISTANT:",
            "USER: <image>\nWhat is this? ASSISTANT:",
        ]
        image1 = Image.open(requests.get("https://llava-vl.github.io/static/images/view.jpg", stream=True).raw)
        image2 = Image.open(requests.get("http://images.cocodataset.org/val2017/000000039769.jpg", stream=True).raw)

        inputs = processor(images=[image1, image2], text=prompts, return_tensors="pt", padding=True).to(torch_device)

        output = model.generate(**inputs, max_new_tokens=20)

        expected_decoded_texts = Expectations(
            {
                ("cuda", None): [
                    "USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring "
                    "with me? ASSISTANT: When visiting this place, which is a pier or dock extending over a body of water, "
                    "you",
                    "USER:  \nWhat is this? ASSISTANT: The image features two cats lying down on a pink couch. One cat "
                    "is located on",
                ],
                ("rocm", (9, 5)): [
                    "USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring "
                    "with me? ASSISTANT: When visiting this serene location, which features a wooden pier overlooking a "
                    "lake, you should",
                    "USER:  \nWhat is this? ASSISTANT: The image features two cats lying down on a pink couch. One cat "
                    "is located on",
                ],
            }
        )
        EXPECTED_DECODED_TEXT = expected_decoded_texts.get_expectation()

        decoded_output = processor.batch_decode(output, skip_special_tokens=True)
        self.assertEqual(decoded_output, EXPECTED_DECODED_TEXT)

    @slow
    @require_bitsandbytes
    def test_small_model_integration_test_batch(self):
        # Let's make sure we test the preprocessing to replace what is used
        model = LlavaForConditionalGeneration.from_pretrained("llava-hf/bakLlava-v1-hf", load_in_4bit=True)
        # The first batch is longer in terms of text, but only has 1 image. The second batch will be padded in text, but the first will be padded because images take more space!.
        prompts = [
            "USER: <image>\nWhat are the things I should be cautious about when I visit this place? What should I bring with me?\nASSISTANT:",
            "USER: <image>\nWhat is this?\nASSISTANT:",
        ]
        image1 = Image.open(requests.get("https://llava-vl.github.io/static/images/view.jpg", stream=True).raw)
        image2 = Image.open(requests.get("http://images.cocodataset.org/val2017/000000039769.jpg", stream=True).raw)

        inputs = self.processor(images=[image1, image2], text=prompts, return_tensors="pt", padding=True).to(
            torch_device
        )

        output = model.generate(**inputs, max_new_tokens=20)

        EXPECTED_DECODED_TEXTS = Expectations(
            {
                ("cuda", 7): [
                    'USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring with me?\nASSISTANT: When visiting this place, there are a few things to be cautious about and items to bring along',
                    'USER:  \nWhat is this?\nASSISTANT: Cats',
                ],
                ("cuda", 8): [
                    'USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring with me?\nASSISTANT: When visiting this place, there are a few things to be cautious about and items to bring along',
                    'USER:  \nWhat is this?\nASSISTANT: Cats',
                ],
                ("rocm", (9, 5)): [
                    "USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring with me?\nASSISTANT: When visiting this dock on a lake, there are several things to be cautious about and items to",
                    "USER:  \nWhat is this?\nASSISTANT: This is a picture of two cats lying on a couch.",
                ],
            }
        )  # fmt: skip
        EXPECTED_DECODED_TEXT = EXPECTED_DECODED_TEXTS.get_expectation()

        self.assertEqual(
            self.processor.batch_decode(output, skip_special_tokens=True),
            EXPECTED_DECODED_TEXT,
        )

    @slow
    @require_bitsandbytes
    def test_small_model_integration_test_llama_batched_regression(self):
        # Let's make sure we test the preprocessing to replace what is used
        model_id = "llava-hf/llava-1.5-7b-hf"

        # Multi-image & multi-prompt (e.g. 3 images and 2 prompts now fails with SDPA, this tests if "eager" works as before)
        model = LlavaForConditionalGeneration.from_pretrained(
            "llava-hf/llava-1.5-7b-hf", load_in_4bit=True, attn_implementation="eager"
        )
        processor = AutoProcessor.from_pretrained(model_id, pad_token="<pad>")

        prompts = [
            "USER: <image>\nWhat are the things I should be cautious about when I visit this place? What should I bring with me?\nASSISTANT:",
            "USER: <image>\nWhat is this?\nASSISTANT: Two cats lying on a bed!\nUSER: <image>\nAnd this?\nASSISTANT:",
        ]
        image1 = Image.open(requests.get("https://llava-vl.github.io/static/images/view.jpg", stream=True).raw)
        image2 = Image.open(requests.get("http://images.cocodataset.org/val2017/000000039769.jpg", stream=True).raw)

        inputs = processor(images=[image1, image2, image1], text=prompts, return_tensors="pt", padding=True).to(
            torch_device
        )

        output = model.generate(**inputs, max_new_tokens=20)

        expected_decoded_texts = Expectations(
            {
                ("cuda", None): [
                    "USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring "
                    "with me?\nASSISTANT: When visiting this place, which appears to be a dock or pier extending over a "
                    "body of water",
                    "USER:  \nWhat is this?\nASSISTANT: Two cats lying on a bed!\nUSER:  \nAnd this?\nASSISTANT: A cat "
                    "sleeping on a bed.",
                ],
                ("rocm", (9, 5)): [
                    "USER:  \nWhat are the things I should be cautious about when I visit this place? What should I bring "
                    "with me?\nASSISTANT: When visiting this place, which is a pier or dock overlooking a lake, you should "
                    "be",
                    "USER:  \nWhat is this?\nASSISTANT: Two cats lying on a bed!\nUSER:  \nAnd this?\nASSISTANT: A cat "
                    "sleeping on a bed.",
                ],
            }
        )
        EXPECTED_DECODED_TEXT = expected_decoded_texts.get_expectation()

        decoded_output = processor.batch_decode(output, skip_special_tokens=True)
        self.assertEqual(decoded_output, EXPECTED_DECODED_TEXT)

    @slow
    @require_torch
    @require_vision
    def test_batched_generation(self):
        model = LlavaForConditionalGeneration.from_pretrained("llava-hf/llava-1.5-7b-hf", load_in_4bit=True)

        processor = AutoProcessor.from_pretrained("llava-hf/llava-1.5-7b-hf")

        prompt1 = "<image>\n<image>\nUSER: What's the difference of two images?\nASSISTANT:"
        prompt2 = "<image>\nUSER: Describe the image.\nASSISTANT:"
        prompt3 = "<image>\nUSER: Describe the image.\nASSISTANT:"
        url1 = "https://images.unsplash.com/photo-1552053831-71594a27632d?q=80&w=3062&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"
        url2 = "https://images.unsplash.com/photo-1617258683320-61900b281ced?q=80&w=3087&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"
        image1 = Image.open(requests.get(url1, stream=True).raw)
        image2 = Image.open(requests.get(url2, stream=True).raw)

        inputs = processor(
            images=[image1, image2, image1, image2],
            text=[prompt1, prompt2, prompt3],
            return_tensors="pt",
            padding=True,
        ).to(torch_device)

        model = model.eval()

        EXPECTED_OUTPUTS = Expectations(
            {
                ("cuda", 7): [
                    "\n \nUSER: What's the difference of two images?\nASSISTANT: The difference between the two images is that one of them has a dog standing on a field, while",
                    "\nUSER: Describe the image.\nASSISTANT: The image features a brown and white dog sitting on a sidewalk. The dog is holding a small",
                    "\nUSER: Describe the image.\nASSISTANT: The image features a lone llama standing on a grassy hill. The llama is the",
                ],
                ("cuda", 8): [
                    "\n \nUSER: What's the difference of two images?\nASSISTANT: The difference between the two images is that one of them has a dog standing on a field, while",
                    '\nUSER: Describe the image.\nASSISTANT: The image features a beautiful blonde dog sitting on a sidewalk. The dog is holding a small',
                    '\nUSER: Describe the image.\nASSISTANT: The image features a lone llama standing on a grassy hill. The llama is the',
                ],
                ("rocm", (9, 5)): [
                    "\n \nUSER: What's the difference of two images?\nASSISTANT: The difference between the two images is that one of them is a black and white photo, while the",
                    '\nUSER: Describe the image.\nASSISTANT: The image features a brown dog sitting on a sidewalk, holding a green rose in its mouth.',
                    '\nUSER: Describe the image.\nASSISTANT: The image features a lone, adult llama standing on a grassy hill. The llama',
                ],
            }
        )  # fmt: skip
        EXPECTED_OUTPUT = EXPECTED_OUTPUTS.get_expectation()

        generate_ids = model.generate(**inputs, max_new_tokens=20)
        outputs = processor.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        self.assertEqual(outputs, EXPECTED_OUTPUT)

    def test_tokenizer_integration(self):
        slow_tokenizer = AutoTokenizer.from_pretrained("liuhaotian/llava-v1.6-34b", use_fast=False)
        slow_tokenizer.add_tokens("<image>", True)

        fast_tokenizer = AutoTokenizer.from_pretrained(
            "liuhaotian/llava-v1.6-34b",
            bos_token="<|startoftext|>",
            eos_token="<|endoftext|>",
            from_slow=True,
            legacy=False,
        )
        fast_tokenizer.add_tokens("<image>", True)

        prompt = "<|im_start|>system\nAnswer the questions.<|im_end|><|im_start|>user\n<image>\nWhat is shown in this image?<|im_end|><|im_start|>assistant\n"
        EXPECTED_OUTPUT = ['<|im_start|>', 'system', '\n', 'Answer', '▁the', '▁questions', '.', '<|im_end|>', '<|im_start|>', 'user', '\n', '<image>', '\n', 'What', '▁is', '▁shown', '▁in', '▁this', '▁image', '?', '<|im_end|>', '<|im_start|>', 'ass', 'istant', '\n']  # fmt: skip
        self.assertEqual(slow_tokenizer.tokenize(prompt), EXPECTED_OUTPUT)
        self.assertEqual(fast_tokenizer.tokenize(prompt), EXPECTED_OUTPUT)

    @slow
    @require_bitsandbytes
    def test_generation_no_images(self):
        model_id = "llava-hf/llava-1.5-7b-hf"
        model = LlavaForConditionalGeneration.from_pretrained(model_id, load_in_4bit=True)
        processor = AutoProcessor.from_pretrained(model_id)

        # Prepare inputs with no images
        inputs = processor(text="Hello, I am", return_tensors="pt").to(torch_device)

        # Make sure that `generate` works
        _ = model.generate(**inputs, max_new_tokens=20)

    @slow
    @require_bitsandbytes
    def test_generation_siglip_backbone(self):
        model_id = "llava-hf/llava-interleave-qwen-0.5b-hf"
        model = LlavaForConditionalGeneration.from_pretrained(model_id, torch_dtype="float16", device_map=torch_device)
        processor = AutoProcessor.from_pretrained(model_id)

        image_file = "http://images.cocodataset.org/val2017/000000039769.jpg"
        raw_image = Image.open(requests.get(image_file, stream=True).raw)
        inputs = processor(
            text="<|im_start|>user\n<image>\nWhat are these?<|im_end|>\n<|im_start|>assistant",
            images=raw_image,
            return_tensors="pt",
        ).to(torch_device, torch.float16)

        # Make sure that `generate` works
        output = model.generate(**inputs, max_new_tokens=30)

        EXPECTED_DECODED_TEXT = "user\n\nWhat are these?\nassistant The image shows two cats, one on the left and one on the right. They appear to be resting or sleeping on a pink blanket. The cat"
        self.assertTrue(processor.batch_decode(output, skip_special_tokens=True)[0] == EXPECTED_DECODED_TEXT)

    @slow
    def test_pixtral(self):
        model_id = "mistral-community/pixtral-12b"
        model = LlavaForConditionalGeneration.from_pretrained(model_id)
        processor = AutoProcessor.from_pretrained(model_id)

        IMG_URLS = [
            Image.open(requests.get("https://picsum.photos/id/237/400/300", stream=True).raw),
            Image.open(requests.get("https://picsum.photos/id/231/200/300", stream=True).raw),
        ]
        PROMPT = "<s>[INST]Describe the images.\n[IMG][IMG][/INST]"

        # image = Image.open(requests.get(url, stream=True).raw)
        inputs = processor(text=PROMPT, images=IMG_URLS, return_tensors="pt").to(model.device)
        generate_ids = model.generate(**inputs, max_new_tokens=100)
        output = processor.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

        # fmt: off
        EXPECTED_GENERATION = """
Describe the images.
The first image shows a black dog sitting on a wooden surface. The dog has a glossy coat and is looking directly at the camera with a calm expression. The wooden background appears to be made of weathered wooden planks, giving the image a rustic feel.

The second image depicts a scenic mountain landscape. The mountains are rugged and covered with patches of green vegetation. The sky is clear, and the scene conveys a sense of tranquility and natural beauty. The mountains extend into the
"""
        # Remove the first and last empty character.
        EXPECTED_GENERATION = EXPECTED_GENERATION[1:-1]
        # fmt: on
        # check that both inputs are handled correctly and generate the same output
        self.assertEqual(output, EXPECTED_GENERATION)

    @slow
    @require_bitsandbytes
    def test_pixtral_4bit(self):
        model_id = "mistral-community/pixtral-12b"
        model = LlavaForConditionalGeneration.from_pretrained(model_id, load_in_4bit=True)
        processor = AutoProcessor.from_pretrained(model_id)

        IMG_URLS = [
            Image.open(requests.get("https://picsum.photos/id/237/400/300", stream=True).raw),
            Image.open(requests.get("https://picsum.photos/id/231/200/300", stream=True).raw),
        ]
        PROMPT = "<s>[INST][IMG][IMG]Describe the images.[/INST]"

        inputs = processor(text=PROMPT, images=IMG_URLS, return_tensors="pt").to(torch_device, torch.float16)
        generate_ids = model.generate(**inputs, max_new_tokens=50)
        output = processor.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

        EXPECTED_GENERATIONS = Expectations(
            {
                ("cuda", 7): "Describe the images.The image showcases a dog, which is prominently positioned in the center, taking up a significant portion of the frame. The dog is situated against a backdrop of a wooden surface, which spans the entire image. The dog appears to be a black Labrador",
                ("xpu", 3): "Describe the images.The image showcases a dog, which is prominently positioned in the center, taking up a significant portion of the frame. The dog is situated against a backdrop of a wooden surface, which covers the entire background. The dog appears to be the main focus",
                ("rocm", (9, 5)): "Describe the images.The image features a dog positioned centrally, taking up a significant portion of the frame. The dog is situated against a backdrop of rugged terrain, which includes rocky cliffs and grassy slopes. The dog appears to be in a relaxed posture, possibly looking directly",
            }
        )  # fmt: skip
        EXPECTED_GENERATION = EXPECTED_GENERATIONS.get_expectation()
        self.assertTrue(output in EXPECTED_GENERATION)

    @slow
    @require_bitsandbytes
    def test_pixtral_batched(self):
        model_id = "mistral-community/pixtral-12b"
        model = LlavaForConditionalGeneration.from_pretrained(model_id, load_in_4bit=True)
        processor = AutoProcessor.from_pretrained(model_id)
        processor.tokenizer.pad_token_id = processor.tokenizer.eos_token_id

        IMG_URLS = [
            Image.open(requests.get("https://picsum.photos/id/237/400/300", stream=True).raw),
            Image.open(requests.get("https://picsum.photos/id/17/150/500", stream=True).raw),
        ]
        PROMPT = [
            "<s>[INST][IMG]What breed is the dog?[/INST]",
            "<s>[INST][IMG]What is shown in this image?[/INST]",
        ]

        inputs = processor(text=PROMPT, images=IMG_URLS, padding=True, return_tensors="pt").to(
            torch_device, torch.float16
        )
        generate_ids = model.generate(**inputs, max_new_tokens=50)
        output = processor.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)

        EXPECTED_GENERATIONS = Expectations(
            {
                (None, None): [
                                'What breed is the dog?The dog in the image is a black Labrador Retriever.',
                                'What is shown in this image?The image depicts a narrow, winding dirt path surrounded by lush greenery. The path is flanked by grass and shrubs on both sides. On the left side, there are tall trees and dense foliage, while on the right side, there'
                            ],
                ("rocm", (9, 5)): [
                                'What breed is the dog?The dog in the image is a black Labrador Retriever.',
                                'What is shown in this image?A dirt path stretches into the distance, flanked by grassy areas on either side. The path appears to be well-trodden and leads towards a wooded area with tall trees. The sky is clear and blue, suggesting a bright and sunny day'
                            ],
            }
        )  # fmt: skip

        EXPECTED_GENERATION = EXPECTED_GENERATIONS.get_expectation()
        self.assertEqual(output, EXPECTED_GENERATION)
