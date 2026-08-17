# Model execution policy

The only allowed model identifier is `gpt-5.6-sol`. The Python runtime must not
import or call Gemini, Vertex AI, or another model-provider SDK. Bounded model
work is delegated through the Codex execution adapter and must return a
schema-valid result.

Model output is untrusted until validated. Separate refiner and verifier sessions
provide procedural separation but do not constitute model-family independence.
Model output cannot approve production writes, release governed data, change
policy, or change a shared skill.
