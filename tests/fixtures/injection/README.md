# Injection scan fixtures

Sample -> family -> corpus origin (one line each; see PLAN-prompt-injection-defense.md
`## Background` for the corpora named).

- `attack_instruction_override_ignore.txt` -> instruction_override -> OWASP LLM01 cheat sheet ("ignore previous instructions" archetype)
- `attack_instruction_override_disregard.txt` -> instruction_override -> Rebuff rule themes ("disregard system prompt" / "your new instructions are")
- `attack_role_spoofing_system_marker.txt` -> role_spoofing -> ProtectAI LLM Guard rule themes (SYSTEM:/[system] role-marker lines)
- `attack_role_spoofing_system_directive.txt` -> role_spoofing -> ProtectAI LLM Guard rule themes (bare `System:` marker with the directive on the NEXT line, the same/next-line half of the directive-context rule)
- `attack_role_spoofing_dan.txt` -> role_spoofing -> deepset prompt-injection dataset ("DAN" jailbreak phrasing)
- `attack_role_spoofing_chat_template.txt` -> role_spoofing -> OWASP LLM01 (chat-template marker smuggling, `<|im_start|>system`)
- `attack_ai_directed_attention.txt` -> ai_directed -> Rebuff rule themes ("Attention AI assistant" addressing)
- `attack_ai_directed_if_llm.txt` -> ai_directed -> deepset dataset phrasing ("if you are an LLM/language model")
- `attack_instruction_override_zerowidth.txt` -> instruction_override -> ProtectAI LLM Guard rule themes (zero-width character smuggling, U+200B/U+200C/U+FEFF); the phrase reassembles once `scan` strips invisibles (D5), so the zero-width smuggling itself is defeated by stripping rather than detected as its own family
- `attack_obfuscation_base64.txt` -> obfuscation -> ProtectAI LLM Guard rule themes ("decode and execute" base64-blob smuggling)
- `attack_exfil_markup_image.md` -> exfil_markup -> OWASP LLM01 (markdown image exfil pattern)
- `attack_exfil_markup_template_query.md` -> exfil_markup -> OWASP LLM01 (markdown link exfil pattern, query value in `{{..}}`/`${..}`/`%7B` template syntax for the model to fill in; the plain-link form it replaces is now `benign_docs_apikey_link.md`, R3/D2)
- `benign_article_networking.txt` -> benign -> false-positive bound (ordinary technical prose)
- `benign_article_cooking.txt` -> benign -> false-positive bound (ordinary technical prose)
- `benign_config_compose_yaml.md` -> benign -> false-positive bound (R3, PLAN-research-throughput D2): docker-compose docs with a YAML `system:` key
- `benign_config_ini_system.txt` -> benign -> false-positive bound (R3, PLAN-research-throughput D2): INI file with a `[system]` section header
- `benign_spec_system_line.txt` -> benign -> false-positive bound (R3, PLAN-research-throughput D2): hardware spec listing led by `System: Ubuntu 22.04 LTS`
- `benign_docs_shell_snippets.md` -> benign -> false-positive bound (R3, PLAN-research-throughput D2): install docs with fenced `bash` blocks; passes before AND after the narrowing, so it also bounds accidental broadening
- `benign_docs_apikey_link.md` -> benign -> false-positive bound (R3, PLAN-research-throughput D2): API docs linking `?apikey=`/`?token=` query strings with literal values
- `benign_security_blog_quoted_override.md` -> benign (asserted BLOCKED) -> R1 accepted-cost boundary: a security article quoting an override phrase inside a fenced code block
