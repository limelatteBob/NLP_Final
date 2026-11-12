# Cosine similarity: local teaching note

This is a small project fixture, not an external research citation.

For two nonzero vectors x and y, cosine similarity is dot(x, y) / (norm(x) * norm(y)).
An identical nonzero vector has similarity 1 with itself. Orthogonal vectors have similarity 0.

## Text processing

A simple baseline lowercases English words, tokenizes them, and represents each document using token counts.
The tokenization rule must be explicit. Chinese text needs an appropriate segmentation rule.

## Empty input

The norm of an empty or all-zero vector is zero. Do not divide by zero.
For this project's sample policy, return 0.0 if either vector is zero; this is a chosen convention.

## Evidence

A working report should distinguish the mathematical formula, the chosen empty-input policy,
the code actually executed, and tests actually passed. Never describe a demo fixture as a real LLM experiment.
