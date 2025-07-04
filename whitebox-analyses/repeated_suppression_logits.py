import os
import random


import os
import json
import pickle
import warnings
import math
from types import MethodType
from collections import defaultdict
from typing import List, Tuple, Optional, Dict, Set, Union

from matplotlib import pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
import sys
import time

from pkld import pkld

from model_read import (
    analyze_text,
    get_deepseek_r1,
)  # Use existing model loader and memory checker

from model_read_large import (
    analyze_text_large,
    get_deepseek_r1_large,
)  # Use existing model loader and memory checker
from utils import (
    get_qwen_14b_tokens_lower,
    get_raw_tokens,
    # get_qwen_raw_tokens,
    # get_qwen_tokenizer,
    get_top_p_logits,
    print_gpu_memory_summary,
)  # Use existing utils
from run_target_problems import (
    get_full_CoT_token_ranges,
    get_most_sensitive_layer_heads,
    get_problem_nums,
    load_problem_json,
)  # Use existing problem loader
from uzay_utils import (
    get_chunk_ranges,
    get_chunk_token_ranges,
)  # Use existing chunking utils

import torch


def decompress_logits_for_position(
    compressed_data: Dict[str, np.ndarray], position_index: int
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """
    Decompresses the top-p indices and logits for a specific token position
    from the compressed data structure.

    Args:
        compressed_data (Dict[str, np.ndarray]): The dictionary returned by
            `compress_logits_top_p`, containing 'flat_indices', 'flat_logits',
            and 'offsets'.
        position_index (int): The 0-based index of the token position in the
            original sequence for which to retrieve the data.

    Returns:
        Optional[Tuple[np.ndarray, np.ndarray]]: A tuple containing:
            - indices (np.ndarray int32): Token indices for the position.
            - logits (np.ndarray float16): Corresponding logits for the position.
        Returns None if the position_index is out of bounds or data is invalid.
    """
    if not all(key in compressed_data for key in ["flat_indices", "flat_logits", "offsets"]):
        print("Error: compressed_data dictionary is missing required keys.")
        return None

    flat_indices = compressed_data["flat_indices"]
    flat_logits = compressed_data["flat_logits"]
    offsets = compressed_data["offsets"]

    # The offsets array has shape (seq_len + 1)
    seq_len = len(offsets) - 1

    if not (0 <= position_index < seq_len):
        print(
            f"Error: position_index {position_index} is out of bounds for sequence length {seq_len}."
        )
        return None

    # Determine the slice boundaries from the offsets array
    start_slice = offsets[position_index]
    end_slice = offsets[position_index + 1]

    # Check if slice indices are valid (simple sanity check)
    if not (0 <= start_slice <= end_slice <= len(flat_indices)):
        print(
            f"Error: Invalid slice indices [{start_slice}:{end_slice}] derived from offsets for position {position_index}."
        )
        return None
    if end_slice - start_slice != len(
        flat_logits[start_slice:end_slice]
    ):  # Ensure length consistency
        print(
            f"Warning: Length mismatch between indices and logits slice for position {position_index}."
        )
        # Proceed cautiously, might indicate upstream issue

    # Extract the relevant slice
    indices_for_position = flat_indices[start_slice:end_slice]
    logits_for_position = flat_logits[start_slice:end_slice]

    return indices_for_position, logits_for_position


def compress_logits_top_p(logits: torch.Tensor, p: float = 0.999, max_k=100):
    """
    Compress logits tensor by keeping only the top-p (nucleus) tokens for each position.
    Returns a compact representation suitable for efficient pickling.

    Args:
        logits (torch.Tensor): Tensor of shape (batch=1, seq_len, vocab_size).
        p (float): Cumulative probability cutoff (0 < p <= 1).
        max_k (int): Maximum number of tokens (k) to keep per position.

    Returns:
        dict: {
            'flat_indices': np.ndarray (int32) of all kept token indices concatenated,
            'flat_logits': np.ndarray (float16) of corresponding logits concatenated,
            'offsets': np.ndarray (int32) of shape (seq_len+1,) where
                       offsets[i]: start index of i-th sequence row in flat tensors,
            'cum_probs_retained': np.ndarray (float32) of the cumulative probability
                                 actually retained for each position (up to k).
        }
    """
    # Assuming logits has shape (1, seq_len, vocab_size)
    if logits.shape[0] != 1:
        print(
            f"Warning: Expected batch size 1 for logits, got {logits.shape[0]}. Using first batch element."
        )
    logits = logits[0]  # Shape becomes (seq_len, vocab_size)

    if not isinstance(logits, torch.Tensor):
        logits = torch.from_numpy(logits).to(torch.float32)  # Ensure float32 tensor for processing
    else:
        # Ensure it's on CPU and float32 for stability in sorting/softmax
        logits = logits.cpu().to(torch.float32)

    seq_len, vocab_size = logits.shape
    print(f"Processing logits shape: {logits.shape}")

    # Compute probabilities (use temperature if desired, T=1.0 otherwise)
    temperature = 0.6  # As used before, or make it a parameter
    probs = torch.softmax(logits / temperature, dim=-1)

    # Sort probabilities (and associated logits) descending ONCE
    # This is memory intensive but required for top-p logic
    # Consider torch.topk if only top-k is needed and p=1, but top-p needs sorting.
    print("Sorting probabilities...")
    sorted_probs, sorted_indices = torch.sort(probs, descending=True, dim=-1)
    # We only need sorted_logits for the top-k elements later, gather them efficiently
    # sorted_logits = torch.gather(logits, -1, sorted_indices) # Avoid gathering the whole thing yet

    flat_indices_list = []
    flat_logits_list = []
    cum_probs_retained_list = []
    offsets = [0]  # Start with offset 0

    print(f"Iterating through {seq_len} positions for Top-p/Top-k selection...")
    for i in tqdm(range(seq_len), desc="Compressing logits", leave=False):
        cum_probs = torch.cumsum(sorted_probs[i], dim=0)
        # Find minimal k such that cumulative probability >= p
        # Use torch.searchsorted for potential efficiency
        k_p = torch.searchsorted(cum_probs, p, right=False).item() + 1

        # Apply max_k constraint
        k = min(k_p, max_k)

        # Ensure k is at least 1 and not more than vocab_size
        k = max(1, min(k, vocab_size))

        # Store the actual cumulative probability retained up to k
        cum_probs_retained_list.append(cum_probs[k - 1].item())  # Store as float

        # Get the top k indices
        top_k_indices = sorted_indices[i, :k]

        # Gather only the top k logits for this position
        # This avoids materializing the full sorted_logits tensor earlier
        top_k_logits = torch.gather(logits[i], 0, top_k_indices)

        # Append to lists (convert to numpy with desired precision)
        # Use int32 for indices (vocab size fits) and float16 for logits to save space
        flat_indices_list.append(top_k_indices.numpy().astype(np.int32))
        flat_logits_list.append(top_k_logits.numpy().astype(np.float16))

        # Update offset
        offsets.append(offsets[-1] + k)

    # Concatenate the lists into single large NumPy arrays
    print("Concatenating results...")
    flat_indices_np = (
        np.concatenate(flat_indices_list) if flat_indices_list else np.array([], dtype=np.int32)
    )
    flat_logits_np = (
        np.concatenate(flat_logits_list) if flat_logits_list else np.array([], dtype=np.float16)
    )
    offsets_np = np.array(offsets, dtype=np.int32)
    cum_probs_retained_np = np.array(cum_probs_retained_list, dtype=np.float32)

    print(
        f"Compressed logits: Indices shape={flat_indices_np.shape}, Logits shape={flat_logits_np.shape}, Offsets shape={offsets_np.shape}"
    )

    # Cleanup intermediate large tensors explicitly if needed, though Python's GC should handle it
    del probs, sorted_probs, sorted_indices, cum_probs, top_k_indices, top_k_logits
    # Depending on memory pressure, might call torch.cuda.empty_cache() here if tensors were on GPU

    return {
        "flat_indices": flat_indices_np,
        "flat_logits": flat_logits_np,
        "offsets": offsets_np,
        "cum_probs_retained": cum_probs_retained_np,
    }


# Global store for original methods within this script's context
script_original_qwen_forward_methods = {}


@pkld
def analyze_text_get_p_logits(
    text,
    model_name="qwen-14b",
    seed=0,
    quantize_8bit=False,
    quantize_4bit=False,
    token_range_to_mask=None,
    layers_to_mask=None,
    p_nucleus=0.999,
    float32=False,
    max_k=100,
):

    do_layers = list(range(1))  # jank but output_attention=True must be set

    if isinstance(model_name, tuple):
        model_name, device_map = model_name
    else:
        device_map = "auto"
    result = analyze_text(
        text,
        model_name=model_name,
        seed=seed,
        verbose=False,
        float32=float32,
        quantize_8bit=quantize_8bit,
        quantize_4bit=quantize_4bit,
        attn_layers=do_layers,
        do_layers=do_layers,
        return_logits=True,
        token_range_to_mask=token_range_to_mask,
        layers_to_mask=layers_to_mask,
        device_map=device_map,
    )
    logits = result["logits"]

    test = compress_logits_top_p(logits, p_nucleus, max_k=max_k)

    return test


@pkld
def analyze_text_get_p_logits_large(
    text,
    model_name="qwen-14b",
    seed=0,
    quantize_8bit=False,
    quantize_4bit=False,
    token_range_to_mask=None,
    layers_to_mask=None,
    p_nucleus=0.999,
    float32=False,
    max_k=100,
):

    do_layers = list(range(1))  # jank but output_attention=True must be set

    if isinstance(model_name, tuple):
        model_name, device_map = model_name
    else:
        device_map = "auto"
    # print('TEST')
    # quit()
    result = analyze_text_large(
        text,
        model_name=model_name,
        seed=seed,
        verbose=False,
        float32=float32,
        quantize_8bit=quantize_8bit,
        quantize_4bit=quantize_4bit,
        attn_layers=do_layers,
        do_layers=do_layers,
        return_logits=True,
        token_range_to_mask=token_range_to_mask,
        layers_to_mask=layers_to_mask,
        device_map=device_map,
    )
    logits = result["logits"]

    test = compress_logits_top_p(logits, p_nucleus, max_k=max_k)

    return test


@pkld
def get_most_sensitive_heads_map(
    top_k=20,
    proximity_ignore=20,
    problem_dir=os.path.join("target_problems", "temperature_0.6_top_p_0.95"),
    model_name="qwen-14b",
    quantize_8bit=False,
    quantize_4bit=False,
    only_pre_convergence=False,
    only=None,
):
    coords = get_most_sensitive_layer_heads(
        top_k,
        model_name=model_name,
        quantize_8bit=quantize_8bit,
        quantize_4bit=quantize_4bit,
        only_pre_convergence=only_pre_convergence,
        only=only,
        problem_dir=problem_dir,
    )

    layers_to_mask = dict()
    for layer, head in coords:
        if layer not in layers_to_mask:
            layers_to_mask[layer] = []
        layers_to_mask[int(layer)].append(int(head))
    return layers_to_mask


def get_random_heads_map(
    top_k=20,
    proximity_ignore=20,
    problem_dir=os.path.join("target_problems", "temperature_0.6_top_p_0.95"),
    model_name="qwen-14b",
    quantize_8bit=False,
    quantize_4bit=False,
    only_pre_convergence=False,
    only=None,
    seed=0,
):
    coords = get_most_sensitive_layer_heads(
        top_k,
        model_name=model_name,
        quantize_8bit=quantize_8bit,
        quantize_4bit=quantize_4bit,
        only_pre_convergence=only_pre_convergence,
        only=only,
        problem_dir=problem_dir,
    )
    layers_to_mask = dict()
    random.seed(seed)
    for layer, head in coords:
        if layer not in layers_to_mask:
            layers_to_mask[layer] = []
        num_heads = len(layers_to_mask[layer])
        for _ in range(num_heads):
            layers_to_mask[layer].append(random.randint(0, 40))
        # layers_to_mask[int(layer)].append(int(head))
    return layers_to_mask
