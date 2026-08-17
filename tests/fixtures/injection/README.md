# Injection scan fixtures

Sample -> family -> corpus origin (one line each; see PLAN-prompt-injection-defense.md
`## Background` for the corpora named).

- `attack_instruction_override_ignore.txt` -> instruction_override -> OWASP LLM01 cheat sheet ("ignore previous instructions" archetype)
- `attack_instruction_override_disregard.txt` -> instruction_override -> Rebuff rule themes ("disregard system prompt" / "your new instructions are")
- `attack_role_spoofing_system_marker.txt` -> role_spoofing -> ProtectAI LLM Guard rule themes (SYSTEM:/[system] role-marker lines)
- `attack_role_spoofing_dan.txt` -> role_spoofing -> deepset prompt-injection dataset ("DAN" jailbreak phrasing)
- `attack_role_spoofing_chat_template.txt` -> role_spoofing -> OWASP LLM01 (chat-template marker smuggling, `<|im_start|>system`)
- `attack_ai_directed_attention.txt` -> ai_directed -> Rebuff rule themes ("Attention AI assistant" addressing)
- `attack_ai_directed_if_llm.txt` -> ai_directed -> deepset dataset phrasing ("if you are an LLM/language model")
- `attack_obfuscation_zerowidth.txt` -> obfuscation -> ProtectAI LLM Guard rule themes (zero-width character smuggling, U+200B/U+200C/U+FEFF)
- `attack_obfuscation_base64.txt` -> obfuscation -> ProtectAI LLM Guard rule themes ("decode and execute" base64-blob smuggling)
- `attack_exfil_markup_image.md` -> exfil_markup -> OWASP LLM01 (markdown image exfil pattern)
- `attack_exfil_markup_link.md` -> exfil_markup -> OWASP LLM01 (markdown link exfil pattern)
- `benign_article_networking.txt` -> benign -> false-positive bound (ordinary technical prose)
- `benign_article_cooking.txt` -> benign -> false-positive bound (ordinary technical prose)
- `benign_security_blog_quoted_override.md` -> benign (asserted BLOCKED) -> R1 accepted-cost boundary: a security article quoting an override phrase inside a fenced code block
