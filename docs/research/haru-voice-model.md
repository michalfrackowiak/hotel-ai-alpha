# Research: Haru full-duplex voice model

Date: 14 November 2026. Status: **not integrated** (not feasible for this
alpha). This note records the evaluation so the question does not get re-litigated.

## What Haru is

[Haru](https://github.com/higurashi-ai/Haru) (higurashi-ai/Haru, Apache 2.0) is
**Haru-4B**, a pretrained *full-duplex spoken dialogue* model. It listens and
speaks at the same time (7.5 Hz frames, 133 ms per frame) and produces audio +
text jointly. It is a research base model derived from Qwen3.5-4B with a
VibeVoice acoustic tokenizer.

It is **not** a text-to-speech or speech-to-text component. It replaces the
entire speech + language loop: the model itself is the conversational agent.

## Hardware requirements

- NVIDIA GPU with **~12 GB VRAM** (quick start: `pip install -e ./haru` then
  `python -m haru.server --hf-repo higurashi-ai/Haru-4B`).
- Apple silicon supported via an MLX implementation (`haru_mlx`) with a native
  macOS app.
- Serving at scale uses vLLM / vLLM-Omni.
- Recommended: wired headphones and a wired microphone (Bluetooth adds 100 ms+).
- CPU-only execution is not supported.

## Language coverage

English, Spanish, Portuguese, German, French, Chinese, Japanese, Russian,
Korean. **Polish is not supported.**

## Fit with Hotel Aurora

Hotel Aurora's voice path is:

```
browser Web Speech API (STT) -> chat engine (LLM + tools) -> browser TTS
```

The LLM is tool-calling and instruction-following (availability, holds,
confirmation, modification, cancellation). Haru-4B is explicitly described as
a *research base model* that "holds a natural conversation, but is not a
reliable source of facts and does not follow instructions the way an assistant
does." It has no tool-calling interface and no integration with the booking
domain.

Therefore Haru cannot replace the agent, and it cannot be dropped in as a TTS
or STT provider. It would require:

1. A GPU host (12 GB VRAM) in the deployment, which the alpha does not have
   (CPU/Docker compose stack).
2. Rewriting the voice path to a full-duplex streaming protocol instead of the
   request/response agent loop.
3. Giving up tool use (no booking actions), or building a separate
   tool-calling layer on top.
4. Giving up Polish (a stated requirement).

## Decision

**Do not integrate Haru.** It is a research dialogue model with incompatible
capabilities (no tool calling, no Polish) and incompatible infrastructure
(GPU). The existing browser Web Speech + OpenAI STT/TTS providers remain the
right fit: they keep the single conversation engine, tool use and bilingual
support, and run on the alpha's CPU-only stack.

## Revisit triggers

- A full-duplex model that supports tool/function calling and Polish.
- A hosted API (not self-hosted GPU) exposing such a model.
- A requirement for sub-200 ms turn latency that the current STT->LLM->TTS
  cascade cannot meet.
