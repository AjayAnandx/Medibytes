# MediBytes — Full Architecture & Tool Fit: Cloud vs Local

> Companion to `PRODUCTION_PLAN_L7.md`. Deep dive on why each tool won, with pricing, benchmarks, and hybrid routing for 1000 machines.
> Languages: EN/HI/TA + Hinglish/Tanglish code-mix. PHI/HIPAA air-gapped.

---

## 1. At-a-Glance Stack

### Cloud (Managed, Autoscaled, BAA)
```
[Mic] -> FFmpeg 16k mono -> DeepFilterNet + Silero VAD
 -> Deepgram Nova-3 Medical (EN/HI multi) / GCP Chirp 3 (TA preview) // Fallback Faster-Whisper
 -> Indic Normalize -> Ensemble: BioClinical ModernBERT-large + MuRIL + HingMBERT
 -> LLM Mapping: GPT-4o / Claude 3.5 (JSON schema)
 -> FerroTERM (FHIR $validate/$lookup) cache + RxNorm/SNOMED/ICD-10
 -> Human Review Gate (Next.js, Redis BullMQ queue)
 -> docx.js (DOCX) -> Gotenberg (Chromium+LibreOffice) -> PDF/A
 -> Postgres (RDS) + S3 (WORM) + Redis (ElastiCache) + OTEL/Prometheus/Grafana + Swagger
 Infra: EKS 1.32 + Karpenter (or AKS 7% cheaper) + A10G/L4 GPU + HPA on queue_depth
```

### Local / Air-Gapped (Docker Compose, No Internet, PHI never leaves)
```
[Mic] -> FFmpeg -> RNNoise (CPU) / DeepFilterNet4 (GPU) + Silero VAD + pyannote (optional diarization)
 -> Faster-Whisper large-v3-int8 (CTranslate2) + whisper.cpp (Apple Silicon)
 -> Same NER ensemble (ONNX quantized) + MedSpaCy ConText + CAN-BERT distilled
 -> LLM: Llama 3.1 70B / Qwen2.5 via vLLM/Ollama Q4_K_M
 -> FerroTERM single binary (89µs ICD-10, 517µs SNOMED, 40MB disk) + local ICD/RxNorm/SNOMED dumps
 -> Same Review UI (PWA offline, IndexedDB draft, bulk-sync)
 -> docx.js -> Gotenberg container or LibreOffice headless direct
 -> Postgres 16-alpine + MinIO + Redis 7 + FerroTERM + Gotenberg in one docker-compose.yml
     docker save/load for air-gapped distribution
```

### Hybrid (Recommended for 99% at Scale)
- **Pluggable interfaces:** `AudioEnhancer`, `STTEngine`, `NERPipeline`, `OntologyValidator`, `TemplateRenderer`, `Storage` — switched by `STT_PROVIDER`, `VALIDATOR` env.
- **Routing:** `if (internet && BAA && lang==ta) -> GCP/Deepgram else -> Faster-Whisper`. Tiered routing saves 10-20% (only premium for noisy/multilingual).
- **Edge draft:** Edge runs `Faster-Whisper small` (<2s) for instant draft, async ships to cloud `large-v3` for verified — reconciliation via Postgres outbox -> RabbitMQ -> MinIO.

---

## 2. Comparison Tables Per Stage (Winner per Stage)

### Stage 1: Audio Enhancement (Denoise, Dereverb, Resample, VAD)

| Tool | Pros | Cons | Pricing | Cloud/Local | Bench | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **FFmpeg** | Ubiquitous resample/mono/loudnorm `loudnorm=I=-16:TP=-1.5:LRA=11`, zero dep | Not AI denoise | Free MIT | Both ✅ | <10ms | **MANDATORY first step** |
| **DeepFilterNet4** | SOTA deep noise+dereverb+echo, Rust+ONNX, CPU/GPU, real-time | 30MB model, needs tuning | Free MIT | Cloud ✅ GPU, Local ✅ | Best for fan/AC ward noise, beats RNNoise 2025 benchmarks | **WINNER quality** |
| **RNNoise (Xiph)** | Ultra-light C, 1-2% CPU, Pi-friendly | Noise only, no dereverb | Free BSD | Local ✅ best CPU-only | ~10ms | **WINNER CPU-constrained** |
| **Krisp SDK** | Commercial best-in-class, VAD built-in | Closed per-seat $$, needs license server | Commercial | Cloud ✅ Local ❌ | High | Reject for air-gap |
| **Dolby.io Enhance** | Single API call denoise | Hosted only, PHI leaves, pay/min | Pay/min | Cloud only | High | Reject HIPAA |
| **Silero VAD + pyannote 3.1** | Silero ONNX 5-10ms light VAD; pyannote SOTA diarization ~90% F1 | pyannote needs 2GB VRAM | Silero MIT, pyannote MIT | Both ✅ | — | **WINNER combo: Silero VAD always + pyannote when speaker attribution needed** |

**Decision:** `FFmpeg + Silero VAD` always, `DeepFilterNet4` when GPU, `RNNoise` fallback CPU, `pyannote` behind flag.

### Stage 2: STT — Core for HI/TA/Code-Mix

| Tool | Pros | Cons | Pricing | Cloud/Local | Bench (Accuracy/Latency/Langs) | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Faster-Whisper large-v3-int8** | Same 2.7% WER (LibriSpeech clean) as vanilla, 4-8x faster, 2.1GB VRAM INT8 vs 5GB FP16, 13.9 rps vs 6.3, p50 632ms vs 1488ms, 99 langs incl hi/ta, CTranslate2 MIT, offline | Needs GPU for large realtime, Tamil accent needs fine-tune | Free self-host (L4 $0.60/hr) | **Local 🏆 PERFECT**, Cloud ✅ cheap batch | RTF 12x RTX 4070, 3x CPU, fits 8GB GPU | **LOCAL WINNER, universal base** |
| **Whisper.cpp** | Best Apple Silicon 10x RT via Metal, C binary, Pi-friendly | No NVIDIA Metal | Free MIT | Local ✅ Mac/edge | Identical WER | **Winner Mac/edge** |
| **Deepgram Nova-3 Medical** | Median WER 3.44%, KER 6.79% (-40%), KRR 93.99%, Keyterm Prompting 100 terms, language=multi live code-switch 10 langs incl HI, sub-300ms, 5-40x faster | Medical variant EN-only; HI via general multi (not medical), no TA in multi, needs BAA | $0.0043/min ($0.258/hr) vs AWS Medical $0.075/min ($4.50/hr) | Cloud ✅ best $/acc | Best cloud medical WER | **CLOUD WINNER EN/HI** |
| **Azure AI Speech** | 100+ langs, custom speech, BAA, 90-96% | No single medical endpoint, config overhead | Standard $0.016/min, Custom $0.027/min | Cloud ✅ | hi-IN, ta-IN | Good if Azure estate |
| **AWS Transcribe Medical** | HIPAA, HealthScribe, 94-96% pharmacy | Weakest Missed Entity Rate bench, EN-only, most expensive | $0.075/min Medical | Cloud ✅ | 100+ langs std but Medical en-only | Overpriced, choose only AWS lock-in |
| **GCP Chirp 3** | 125+ langs widest, hi-IN GA, ta-IN preview, medical+phone, Vertex AI | Chirp ta preview != GA, ~11.6% WER vs 5.26% Deepgram bench | $0.016/min, $4/1K at >2M | Cloud ✅ | Best breadth but lags accuracy | **Best for Tamil until Deepgram adds ta** |

**Tamil gap:** Deepgram Medical is EN-only, Nova-3 multi lacks TA — so Tamil must use `GCP Chirp 3 preview` or `Faster-Whisper`. **Verdict:** Faster-Whisper large-v3-int8 is universal base. Cloud primary: Deepgram Nova-3 (HI+EN) + GCP Chirp (TA) with Whisper fallback. Cost `faster-whisper $0.0048/audio-hr on L4` vs Deepgram $0.258/hr.

### Stage 3: NER — Template-Aware Clinical Entities

| Tool | Pros | Cons | Pricing | Cloud/Local | Bench | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **BioClinical ModernBERT-large** | SOTA 90.8% ChemProt, 95.1% COS, 60.8% Phenotype, 8192 ctx (vs 512 old), 53.5B tokens PubMed+PMC+MIMIC | EN-only, MIMIC bias | Free Apache | Both ✅ | Best EN clinical | **🏆 EN WINNER self-host primary** |
| **PubMedBERT / BioMedRoBERTa** | PubMedBERT 0.8339 med NER, BioMedRoBERTa 0.8468 macro | Needs fine-tune, 512 ctx | Free | Both | 93.76% BioRED ensemble | Ensemble base |
| **MuRIL-large-cased** | Transliteration-trained 17 langs, PANX Tamil 71.1 vs XLM-R 59.5, HI NER 78.3 vs 73.0, Hinglish 84.2% vs XLM-R 79.2% | Tamil 61.98 base lower | Free Apache | **Local 🏆 code-mix king** | Best hi/ta mix | **🏆 Code-mix WINNER** |
| **HingMBERT/HingBERT** | HingMBERT 77.14 F1 > MuRIL 73.51 > mBERT 71.04 on Hinglish | Hinglish-only | Free | Local ✅ | +3.6pp | **Use for Hinglish slot** |
| **spaCy + scispaCy** | Fast, rule-friendly | Lower F1 60-70 base | Free MIT | Local ✅ | Fast 10x | Framework *around* BERT, not core |
| **MedSpaCy + NegSpaCy** | Rule en_clinical, fast, offline | NegEx F1 0.492, P 0.356 vs transformer 0.777/0.768 | Free | Local ✅ | High recall but catastrophic precision | **Fallback only, not primary negation** |
| **GPT-4o / Claude 3.5** | Best zero-shot mapping, JSON function calling, 31% missed term cut few-shot | Closed, BAA limited, $6-15/1M tokens, latency 1-2s | Pay/token | Cloud ✅ only | GPT-4o SFT F1 87.1% CADEC | **For mapping (stage 4), not NER boundary** |
| **Llama 3.1 70B / Qwen2.5 local** | Self-host vLLM/Ollama Q4 24GB A10G, HIPAA safe | Lower nuance | Free self-host | **Local 🏆** | Needs fine-tune (zero-shot 62 vs 77 fine-tuned) | **LOCAL LLM WINNER for mapping** |

**Verdict:** Ensemble `BioClinical ModernBERT (EN) + MuRIL/HingMBERT (code-mix) + Llama local` for local; cloud adds `GPT-4o` for mapping but gated by validation.

### Stage 4-5: Validation — FHIR Ontology + Dose Rules

| Tool | Pros | Cons | Pricing | Cloud/Local | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **FerroTERM** | ICD-10 lookup 89-104µs, SNOMED 517-934µs, 40-60MB disk, 45-402MB RAM, single distroless binary, SLSA L3, FHIR $lookup/$validate/$subsumes/$expand/$translate | SNOMED RF2 bring-your-own (license) | Free BSL | **Both ✅ but Local ESSENTIAL** | **🏆 WINNER speed** |
| **Snowstorm Lite** | 500MB RAM Lucene, Spring Boot, ECL search | Heavier, JVM, needs Lucene | Free Apache | Both | Alt for ECL search |
| **fhir-codebridge** | 123K pre-loaded, UMLS 600K, Docker, Prometheus, /validate, /bulk | SNOMED requires UMLS | Free MIT | Both | **🏆 Bulk/mapping UI** |
| **Outburn FTR** | Offline ValueSet expand, no external server | — | Free | Local ✅ | **ValueSet caching** |
| **RxNorm NLM, ICD-10 CMS, SNOMED CT** | RxNorm 1.41ms, 72MB; ICD-10 74K public; SNOMED 600K UMLS | NLM API needs net — use local dump | Free (UMLS key) | Cloud API vs local dump | Local dump + nightly sync |

**Decision:** `FerroTERM + fhir-codebridge + FTR` offline trio covers `µs lookup + bulk + ValueSet`.

### Stage 6-7: Template + Export

| Tool | Pros | Cons | Pricing | Cloud/Local | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **python-docx / docx.js** | Native DOCX gen, docx.js 312KB no browser MIT, Jinja2 placeholders, version-safe | Manual styling | Free MIT | **Both ✅** | **🏆 DOCX creation — always gen DOCX natively** |
| **Gotenberg 8.x** | Single Docker HTTP API wrapping Chromium+LibreOffice+Pandoc, webhook, PDF/A, merge, stateless | 2-4GB RAM/Chromium conv, 1 LO at a time default, ~1GB image | Free MIT self-host | Both ✅ but container | **🏆 WINNER DOCX->PDF service** |
| **LibreOffice headless direct** | Best Office fidelity, MCP | Concurrency race, 1-3s startup, 500MB-1GB | Free MPL | Both CLI only | Use inside Gotenberg; direct only if no Docker |
| **Puppeteer + sparticuz/chromium** | Exact Chrome render | 66.4MB npm + Chrome 152 vs 149 drift, Node 22.17 req | Free Apache | Cloud long-lived only | **Avoid — Gotenberg is service-owned version** |
| **Aspose/Nutrient** | Highest fidelity | $500-2000/yr, 50-200MB binary | Commercial | Both but cost | Only if tagged PDF/A > Gotenberg |

**Verdict:** `docx.js -> Gotenberg PDF` when PDF needed; fallback direct LibreOffice for air-gap without Gotenberg.

### Stage 8: Infra, Scale, API, Observability

| Tool | Pros | Cons | Pricing | Verdict for 1000 |
| :--- | :--- | :--- | :--- | :--- |
| **Docker + Compose** | 45 lines <1min deploy vs 200+ K8s, docker save/load air-gap, low ops 14h/wk vs 22 EKS | No autoscale, single host | Free | **🏆 LOCAL WINNER 1-4 servers** |
| **K8s EKS 1.32 / GKE Standard / AKS** | 5000 max GKE vs 1500-2000 EKS, HPA, GPU operator, Karpenter 78-85% binpack vs GKE Autopilot 85-92% | 30min setup, EKS $12.8k/mo control vs GKEfree/AKSfree | Managed $73 ctrl, node cost dominates: 1000 m7g.16xlarge $112k-118k/mo | **🏆 CLOUD WINNER >5 servers:** EKS best balance, AKS cheapest 7-10% 200-500 nodes, Autopilot only if 68% toil cut worth 22% premium |
| **GPU** | L4 24GB $0.60/hr, T4 16GB cheapest, A10G 24GB balanced LLM+ASR | A100/H100 overkill STT | Spot $0.0134/vCPU vs $0.0445 on-demand | T4/L4 STT-only, A10G combined, A100 only 1000 concurrent |
| **Queue: Redis BullMQ vs RabbitMQ** | BullMQ JS-native rate limit/retries/UI; RabbitMQ transactional outbox fencing, durability | Redis persistence risk AOF; Rabbit complex | Both OSS | **Hybrid: BullMQ job queue + RabbitMQ outbox fencing (healthcare-grade)** |
| **Storage: MinIO vs S3** | MinIO S3-compat single binary, docker-compose; S3 11 9s BAA | MinIO AGPL concern 2025, S3 egress $0.01-0.02/GB | MinIO free self-host, S3 pay/GB | **Local MinIO, Cloud S3, Hybrid mc mirror** |
| **DB: Postgres 16-alpine** | ACID, pgvector, healthcheck pg_isready | Not TSDB | Free | **WINNER both** |
| **API Docs** | OpenAPI 3.1 + Swagger + Postman | Maintenance | Free | **Both mandatory** |
| **Observability** | Prometheus/Grafana + OTEL + Sentry | OTEL overhead | Free OSS | **WINNER OTEL+Prom/Graf/Sentry+Loki** |

---

## 3. Hybrid Routing & Cost

**Routing logic:**
```
if air_gapped or !internet: -> Faster-Whisper + FerroTERM local
else if language == "ta-IN" and cloud_enabled: -> GCP Chirp 3 (ta preview) // Deepgram lacks ta
else if language in ["hi-IN","en-IN","mix"] and phi_baa_signed: -> Deepgram Nova-3 multi (HI) + Nova-3 Medical (EN)
else: -> Faster-Whisper large-v3-int8 (cheapest, PHI-safe)
```

**Cost vs AWS Medical:**
- Deepgram Nova-3 Medical `$0.0043/min` vs AWS Transcribe Medical `$0.075/min` — **17.4x cheaper**.
- Faster-Whisper self-host `$0.0048/audio-hr on L4` — **cheapest batch**.
- Tiered routing (only noisy/multilingual to premium) saves 10-20%.

**Fleet scaling note:** Fleet of 1000 hospital desktops = **edge Compose per machine + central replica** (not 1000-node K8s cluster $115k/mo). If central, EKS Karpenter binpack 78-85% vs GKE Autopilot 85-92% but 10-22% premium.

---

## 4. When to Choose Which

| Need | Choose |
| :--- | :--- |
| Fastest time-to-99% in cloud, Microsoft estate | Azure AI Speech |
| Fastest time-to-99% in cloud, AWS estate but avoid $4.50/hr | Deepgram Nova-3 + Faster-Whisper fallback (not AWS Medical — weakest entity rate) |
| Tamil code-mix | Faster-Whisper (offline GA) or GCP Chirp 3 (cloud preview) |
| Air-gapped HIPAA, no BAA | Faster-Whisper + MuRIL + Llama + FerroTERM — fully OSS |
| Cheapest batch | Faster-Whisper on L4 spot |
| Highest fidelity PDF/A | Gotenberg (not Puppeteer) |

---

*Sources: Faster-Whisper bench 2026-04-14/15, PromptQuorum 2026-08-28, Deepgram Nova-3 whitepaper/nova-3-medical, AWS/Azure/GCP pricing, MuRIL/HingMBERT studies 2025-09-02, CAN-BERT PMC12092861 2024-09-25, BioClinical ModernBERT 2025-06, FerroTERM.eu, BigIron 2026-05-10, LeanOps 2026-05-06, johal.in 2026-05-06.*
