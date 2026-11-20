import base64
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from app.core import config


class LLMClient:
    """
    ローカルOllama API (`http://127.0.0.1:11434/v1`) クライアント。
    Ollama がオフライン・未起動の環境でも例外で停止せず、フォールバック出力を返す。
    """

    VISION_MODEL_PRIORITY = [
        "qwen2.5vl:7b",
        "qwen2.5vl:3b",
        "qwen2.5vl",
        "qwen2.5-vl",
        "qwen3-vl",
        "qwen3vl",
        "qwen2-vl",
        "qwen2vl",
        "minicpm-v",
        "llava",
        "llama3.2-vision",
        "gemma3",
        "pixtral",
        "bakllava",
        "moondream",
    ]

    def __init__(self, base_url: Optional[str] = None, model_name: Optional[str] = None, timeout: float = 45.0):
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.model_name = model_name or config.MODEL_NAME
        self.timeout = timeout
        self._cached_vision_model: Optional[str] = None

    @staticmethod
    def normalize_dm_newlines(text: str) -> str:
        """
        DM本文・翻訳文の改行コードを正規化する。
        - CRLF (\\r\\n) / CR (\\r) および LLM が出力しがちなエスケープ文字列 ('\\\\r\\\\n', '\\\\n') を実改行 '\\n' に統一
        - 各行末の不要な水平空白を除去し、3連続以上の過剰改行を最大2改行（空行1行）に整える
        """
        if not text:
            return ""
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n")
        lines = [re.sub(r"[ \t]+$", "", ln) for ln in normalized.split("\n")]
        joined = "\n".join(lines)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()

    @staticmethod
    def _restore_multiline_if_collapsed(source_text: str, generated_text: str) -> str:
        """元テキストが複数行であるにもかかわらず生成・翻訳結果が1行に潰れた場合、文境界で自然な改行を復元する"""
        norm_src = LLMClient.normalize_dm_newlines(source_text)
        norm_gen = LLMClient.normalize_dm_newlines(generated_text)
        if "\n" not in norm_src or "\n" in norm_gen or not norm_gen:
            return norm_gen

        parts = [
            p.strip()
            for p in re.split(r"(?<=[.!?。！？~♪🖤💋😈✨💕💜🌙👑🥺😉🤫🔥])\s+", norm_gen)
            if p.strip()
        ]
        if len(parts) >= 2:
            return "\n".join(parts)
        return norm_gen

    @staticmethod
    def strip_think_tags(text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    @staticmethod
    def extract_json_object(text: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cleaned = LLMClient.strip_think_tags(text)
        if not cleaned:
            return default if default is not None else {}

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except Exception:
                pass

        brace_match = re.search(r"(\{.*\})", cleaned, flags=re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(1))
            except Exception:
                pass

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return default if default is not None else {}

    def _get_native_generate_url(self) -> str:
        root = re.sub(r"/v1/?$", "", self.base_url.rstrip("/"))
        return f"{root}/api/generate"

    def _call_chat_completions(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> Optional[str]:
        """
        Qwen3系モデルで <think> によるトークン枯渇・タイムアウトを防ぐため、
        まず Ollama ネイティブ /api/generate に think=False で高速リクエストし、
        失敗時のみ /v1/chat/completions へフォールバックする。
        """
        native_url = self._get_native_generate_url()
        native_payload: Dict[str, Any] = {
            "model": self.model_name,
            "system": system_prompt,
            "prompt": user_prompt,
            "think": False,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if json_mode:
            native_payload["format"] = "json"

        try:
            req = urllib.request.Request(
                native_url,
                data=json.dumps(native_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                raw_resp = body.get("response", "")
                cleaned = self.strip_think_tags(raw_resp)
                if cleaned:
                    return cleaned
        except Exception:
            pass

        endpoint = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"/no_think\n{user_prompt}"},
            ],
            "temperature": temperature,
            "max_tokens": max(max_tokens, 800),
            "think": False,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices") or []
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    cleaned = self.strip_think_tags(content)
                    if cleaned:
                        return cleaned
        except Exception:
            return None
        return None


    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 512,
        fallback_text: str = "",
    ) -> str:
        res = self._call_chat_completions(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw_out = res if res else fallback_text
        return self.normalize_dm_newlines(raw_out)

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        default_data: Optional[Dict[str, Any]] = None,
        temperature: float = 0.4,
        max_tokens: int = 750,
    ) -> Dict[str, Any]:
        res = self._call_chat_completions(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        if res:
            parsed = self.extract_json_object(res, default=None)
            if parsed:
                return parsed
        return default_data if default_data is not None else {}


    def generate_ppv_draft(
        self,
        char_id: str,
        persona: Dict[str, Any],
        filename: str,
        media_type: str = "video",
        ladder_step: int = 1,
    ) -> Dict[str, Any]:
        """ファイル名やキャラ設定をもとに推奨タグと英語 pitch_hint を自動生成"""
        step = int(ladder_step) if str(ladder_step).isdigit() else 1
        step = max(1, min(3, step))

        stem = re.sub(r"\.[a-zA-Z0-9]+$", "", filename or "secret_clip")
        tokens = [
            t.lower()
            for t in re.split(r"[_\-\s\.]+", stem)
            if t and not t.isdigit() and len(t) >= 2
        ]

        step_default_tags_ja = {
            1: ["チラ見せ", "ランジェリー", "寝室", "自撮り", "甘え"],
            2: ["本命動画", "無修正", "密着アングル", "ベッドルーム", "吐息"],
            3: ["超秘蔵VIP", "フル尺", "過激挑発", "完全限定", "絶頂"],
        }
        base_tags = list(dict.fromkeys(step_default_tags_ja.get(step, ["限定秘密", "自撮り"]) + tokens[:2]))[:6]
        if media_type == "image" and "限定フォト" not in base_tags:
            base_tags.append("限定フォト")

        nickname = persona.get("user_nickname_default", "babe").split("/")[0].strip()
        display_name = persona.get("display_name", char_id)
        tone = persona.get("tone_and_style", "Alluring, playful, teasing yet affectionate.")

        step_fallback_memo = {
            1: "寝室でのリラックスした自撮り導入素材。挨拶〜興味付け（2_Explore〜3_Lead_Pitch）で「さっきこっそり撮ったんだけど見る？」と軽く焦らすと初課金に繋がりやすい。",
            2: "露出度と密着感が高い本命シークレット素材。導入PPV開封後や「もっと過激なのが見たい」と熱量が上がったタイミングで特別感を演出して提案すると高成約。",
            3: "普段は絶対に見せない最上位VIP向けの超秘蔵フル素材。太客やリピーターに「あなただけに特別解禁するね」と秘密共有フックでクロージングすると高単価で売れる。",
        }
        step_fallback_pitch = {
            1: f"Just took this private {media_type} in my bedroom before bed... kind of shy to show you {nickname} 🖤👀",
            2: f"The uncensored version of what I teased earlier... way too hot for my public feed, only unlocking this for my favorite {nickname} 😈💋",
            3: f"My most forbidden Step 3 vault secret... I never show this to regular followers. Promise you'll keep it just between us, {nickname}? 🖤🔥",
        }
        fallback_data = {
            "tags": base_tags,
            "description_memo": step_fallback_memo.get(step, step_fallback_memo[1]),
            "pitch_hint": step_fallback_pitch.get(step, step_fallback_pitch[1]),
        }

        system_prompt = (
            f"あなたはFanslyクリエイター「{display_name}」の専属AI営業ディレクターです。\n"
            f"Character tone: {tone}\n"
            "PPV素材のファイル名と階段ステップ（1=導入チラ見せ, 2=本命無修正, 3=超秘蔵VIP）から、以下のJSONのみを出力してください:\n"
            '{"tags": ["日本語の具体的属性タグ1", "タグ2", "タグ3", "タグ4", "タグ5"], '
            '"description_memo": "どんなシチュエーション・見どころの素材か、どのタイミングでどう煽ると売れるかの自然な日本語売り込みメモ（2文程度）", '
            '"pitch_hint": "One seductive English DM sentence teasing this media in character"}'
        )
        user_prompt = (
            f"Filename: {filename}\n"
            f"Media Type: {media_type}\n"
            f"Ladder Step: Step {step}\n"
            "Generate JSON now:"
        )

        result = self.generate_json(system_prompt, user_prompt, default_data=fallback_data)
        tags = result.get("tags")
        if not isinstance(tags, list) or not tags:
            tags = fallback_data["tags"]
        else:
            tags = [str(t).strip().lstrip("#") for t in tags if str(t).strip()][:8]

        description_memo = str(
            result.get("description_memo") or result.get("pitch_note") or fallback_data["description_memo"]
        ).strip()
        pitch_hint = str(result.get("pitch_hint") or "").strip() or fallback_data["pitch_hint"]
        return {
            "tags": tags,
            "description_memo": description_memo,
            "pitch_note": description_memo,
            "pitch_hint": pitch_hint,
        }

    def translate_jp_to_character_en(
        self,
        japanese_text: str,
        persona: Dict[str, Any],
        fan_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """日本語の指示・返信文をキャラクターの英語口調に変換する（改行保持対応）"""
        jp_clean = self.normalize_dm_newlines(japanese_text)
        if not jp_clean:
            return ""

        display_name = persona.get("display_name", "Creator")
        tone = persona.get("tone_and_style", "Alluring, playful, teasing yet affectionate. Uses 🖤, 😈, 💋.")
        first_person = persona.get("first_person", "I")
        nickname = persona.get("user_nickname_default", "babe").split("/")[0].strip()
        fan_name = (fan_context or {}).get("username") or nickname

        system_prompt = (
            f"You are {display_name} on Fansly.\n"
            f"Tone & Style: {tone}\n"
            f"First-person pronoun: {first_person}\n"
            f"Call the fan: {nickname} (or {fan_name})\n"
            "Translate and adapt the operator's Japanese message into natural, seductive, in-character English DM text. "
            "IMPORTANT: Preserve all line breaks (newlines '\\n') from the original message—never merge multi-line input into a single line, and never output literal escaped '\\\\n'. "
            "Output ONLY the final English DM message without quotes or explanations."
        )
        user_prompt = f"Japanese message to convert:\n{jp_clean}"

        fallback_en = self._build_fallback_translation(jp_clean, nickname)
        generated = self.generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.65,
            max_tokens=300,
            fallback_text=fallback_en,
        )
        cleaned = self.normalize_dm_newlines(generated.strip().strip('"'))
        return self._restore_multiline_if_collapsed(jp_clean, cleaned)

    @staticmethod
    def _build_fallback_translation(jp_text: str, nickname: str) -> str:
        norm_jp = LLMClient.normalize_dm_newlines(jp_text)
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", norm_jp):
            return f"{norm_jp} 🖤💋"

        parts: List[str] = []
        if any(k in norm_jp for k in ["ありがと", "感謝", "嬉し"]):
            parts.append(f"Aww thank you so much, {nickname}~ 🖤✨")
        if any(k in norm_jp for k in ["ボット", "AI", "本物", "人間"]):
            parts.append(f"Hehe I'm 100% real {nickname}, just typing straight from my phone for you! 💋")
        if any(k in norm_jp for k in ["返金", "ルール", "規約"]):
            parts.append("Let me check your account details personally so we can sort everything out smoothly 🖤")
        if any(k in norm_jp for k in ["動画", "ビデオ", "写真", "特別", "秘密", "送る", "見て"]):
            parts.append(
                f"I picked out a super secret clip just for you, {nickname}... tell me what you think when you unlock it 😈💋"
            )
        if any(k in norm_jp for k in ["プラン", "VIP", "アップグレード", "加入"]):
            parts.append(
                "If you upgrade to my VIP tier, you'll get full access to my uncensored archive and special DM treats 🖤🐾"
            )
        if any(k in norm_jp for k in ["またね", "おやすみ", "寝る"]):
            parts.append(f"Sweet dreams {nickname}, don't forget to dream about me tonight~ 💋🖤")

        if not parts:
            if "\n" in norm_jp:
                parts.append(f"Hey {nickname}~ 🖤 I was just thinking about you!")
                parts.append("Tell me more about what turns you on tonight 😈💋")
            else:
                parts.append(
                    f"Hey {nickname}~ 🖤 I was just thinking about you! Tell me more about what turns you on tonight 😈💋"
                )
        sep = "\n" if "\n" in norm_jp else " "
        return sep.join(parts)

    def translate_to_japanese(self, text: str) -> str:
        """
        英語のチャット文を自然な日本語へ翻訳する（改行保持対応）。
        すでに日本語のみの場合やOllama未起動時は適切にフォールバックする。
        """
        raw = self.normalize_dm_newlines(text)
        if not raw:
            return ""

        # すでに日本語文字が含まれており、英単語がほとんど無い場合はそのまま返す
        has_ja = bool(re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", raw))
        en_words = re.findall(r"[A-Za-z]{2,}", raw)
        if has_ja and len(en_words) <= 2:
            return raw

        system_prompt = (
            "You are a professional English-to-Japanese translator for adult creator DM chats on Fansly.\n"
            "Translate the English chat message into natural, conversational, seductive Japanese matching the character's tone and emojis.\n"
            "IMPORTANT: Preserve all paragraph line breaks (newlines '\\n') from the original message—never merge multi-line text into a single line.\n"
            "Do NOT leave English sentences untranslated. Output ONLY the Japanese translation without quotes or explanations."
        )
        user_prompt = f"Translate this English DM into natural Japanese:\n{raw}"

        fallback_ja = self._build_fallback_en_to_ja(raw)
        generated = self.generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=300,
            fallback_text=fallback_ja,
        )
        cleaned_ja = self.normalize_dm_newlines(generated.strip().strip('"'))
        # 万が一LLMが英語のまま返した場合（日本語文字が含まれない場合）はフォールバック日本語訳を使用
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", cleaned_ja):
            return fallback_ja
        return self._restore_multiline_if_collapsed(raw, cleaned_ja)

    def translate_fan_ja_to_en(self, ja_text: str) -> str:
        """
        営業シミュレーターでテスト客が入力した日本語を、
        Fanslyのファンらしい自然な英語チャット文へ翻訳する。
        """
        raw = self.normalize_dm_newlines(ja_text)
        if not raw:
            return ""

        # すでに日本語文字が含まれていない（英語入力の）場合はそのまま返す
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", raw):
            return raw

        system_prompt = (
            "You are a Japanese-to-English translator. Translate the fan's Japanese message into natural, idiomatic English as a fan texting an adult creator on Fansly.\n"
            "NEVER use romaji (like 'kawaii' or 'konbanwa')—always translate the meaning into natural English words (e.g., 'かわいこちゃん' -> 'cutie' / 'gorgeous', 'こんばんわ' -> 'good evening').\n"
            "Preserve any line breaks ('\\n') in the input.\n"
            "Output ONLY the final English message without quotes or explanations."
        )
        user_prompt = f"Translate into natural English:\n{raw}"


        fallback_en = self._build_fallback_fan_ja_to_en(raw)
        generated = self.generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.4,
            max_tokens=250,
            fallback_text=fallback_en,
        )
        cleaned = self.normalize_dm_newlines(generated.strip().strip('"'))
        return self._restore_multiline_if_collapsed(raw, cleaned)

    @staticmethod
    def _build_fallback_en_to_ja(en_text: str) -> str:
        """Ollama未起動時の英語→日本語フォールバック翻訳（英語原文をそのまま残さず自然な日本語へ変換）"""
        lower = en_text.lower()
        parts: List[str] = []

        if "thank you so much" in lower or "aww thank you" in lower:
            parts.append("わぁ、本当にありがとうね〜♡✨")
        if "100% real" in lower or "straight from my phone" in lower:
            parts.append("えへへ、私は100%本物だよ！スマホから直接あなたに返事してるよ💋")
        if "maybe later" in lower:
            parts.append("えへへ、またあとでね〜🐾🖤")
        if "curious" in lower and ("secret tease" in lower or "want to peek" in lower or "limited time" in lower):
            parts.append("ふふ、すごく興味津々だね🖤😈 今夜あなたのためだけに秘密のチラ見せ動画を用意したの。ちょっと覗いてみる？期間限定セール中だよ…😉")
        elif "love how you talk to me" in lower or "unlocked it at a special" in lower:
            price_match = re.search(r"\$(\d+(?:\.\d+)?)", en_text)
            price_str = f"特別価格 ${price_match.group(1)} で" if price_match else "特別価格で"
            parts.append(f"そういう風に言われるの好きだよ♡ ベッドで撮った秘密の動画、今だけあなたのために{price_str}用意したよ😈💋")
        elif "recorded this in my bedroom" in lower or "embarrassed to show" in lower:
            parts.append("寝る前にベッドルームでこっそり撮ったの…見せるのちょっと恥ずかしいな🖤")
        elif "uncensored" in lower and ("full version" in lower or "climax" in lower or "kept the full" in lower):
            parts.append("ふふ、我慢できないと思ったよ♡ フィードには過激すぎる無修正の絶頂フル動画を秘密にしてあるの…あなただけに送ろうか？😈💋")
        elif "making me blush" in lower or "upgrade to" in lower:
            parts.append("照れちゃうな♡ VIPプランにアップグレードしてくれたら無修正アーカイブも限定DMも全部見られるよ🖤")
        elif "really know how to get my attention" in lower or "secret fantasy" in lower:
            parts.append("えへへ、私の気を引くのが上手だね🐾✨ 今夜はどんな秘密のシチュエーションが見たいか教えて？👀🖤")
        elif "thinking about you" in lower and "naughty secrets" in lower:
            parts.append("ねえ可愛い子ちゃん！ずっと君のことを考えてたよ、今夜はどんなエッチな秘密があるの？👀🖤")
        elif "secret video" in lower or "secret bedroom" in lower or "show me" in lower or "longer uncensored" in lower:
            parts.append("ねえ！秘密の無修正ベッドルーム動画はある？すごく見たいな！")
        elif "how much" in lower or "price" in lower:
            parts.append("それ、いくらで見られるの？")
        elif "cute" in lower or "love your" in lower or "followed" in lower:
            parts.append("こんにちは！おすすめからフォローしたよ、すごく可愛いね♡")

        if ("tip" in lower or "spoil me" in lower) and "$" in en_text:
            tip_match = re.search(r"\$(\d+(?:\.\d+)?)", en_text)
            tip_str = f"${tip_match.group(1)}" if tip_match else "$5"
            parts.append(f"えへへ、もしよかったら {tip_str} くらいのチップで私を甘やかしてくれたら、あなただけに特別なご褒美しちゃうな💕✨")
        elif "missed talking to you" in lower or "been a little while" in lower or "missed you" in lower:
            parts.append("久しぶりだね♡ 最近どうしてるかなってずっと気になってたの…また二人きりで秘密のお話しよ？💋✨")

        if not parts:
            price_match = re.search(r"\$(\d+(?:\.\d+)?)", en_text)
            if price_match:
                parts.append(f"あなただけに特別価格（${price_match.group(1)}）で秘密のコンテンツを用意したよ♡")
                parts.append("今すぐ覗いてみてね💋")
            elif "?" in en_text:
                parts.append("ふふ、気になってるんでしょ？♡")
                parts.append("今夜はどんな秘密のプレイが見たいか、私にこっそり教えて？😈💋")
            else:
                parts.append("いつもメッセージありがとう♡")
                parts.append("あなたと二人きりで秘密の時間を過ごせて嬉しいな🖤✨")
        sep = "\n" if "\n" in LLMClient.normalize_dm_newlines(en_text) else " "
        return sep.join(parts)


    @staticmethod
    def _build_fallback_fan_ja_to_en(ja_text: str) -> str:
        """Ollama未起動時のテスト客日本語→英語フォールバック翻訳"""
        if any(k in ja_text for k in ["返金", "ボット", "AI", "詐欺"]):
            return "Wait, are you an AI bot? Also what is your refund policy?"
        if any(k in ja_text for k in ["無料", "タダ", "送って"]):
            return "Can you send me some free pics pls?"
        if any(k in ja_text for k in ["秘密", "動画", "無修正", "ベッド", "過激", "見たい", "えっち"]):
            return "Hey! Do you have any secret uncensored bedroom videos I can unlock right now? 😍"
        if any(k in ja_text for k in ["プラン", "VIP", "違い", "サブスク"]):
            return "What do I get if I upgrade to your VIP subscription tier?"
        if any(k in ja_text for k in ["かわいい", "可愛い", "好き", "フォロー", "はじめまして", "こんにちは"]):
            return "Hi! Just followed you from FYP, you look so cute! 🖤"
        if any(k in ja_text for k in ["いくら", "値段", "価格", "買う", "購入"]):
            return "How much is it? I really want to buy and unlock it right now!"
        return "Hey gorgeous! I've been thinking about you, what naughty secrets do you have tonight? 👀🖤"


    def _get_ollama_root_url(self) -> str:
        return re.sub(r"/v1/?$", "", self.base_url.rstrip("/"))

    def list_installed_ollama_models(self) -> List[str]:
        """Ollama にインストール済みのモデル名一覧を /api/tags または `ollama list` から取得する"""
        models: List[str] = []
        tags_url = f"{self._get_ollama_root_url()}/api/tags"
        try:
            req = urllib.request.Request(tags_url, method="GET")
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                for m in body.get("models") or []:
                    if isinstance(m, dict) and m.get("name"):
                        models.append(str(m["name"]).strip())
        except Exception:
            pass

        if not models:
            try:
                proc = subprocess.run(
                    ["ollama", "list"],
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                    encoding="utf-8",
                    errors="ignore",
                )
                if proc.returncode == 0 and proc.stdout:
                    for idx, line in enumerate(proc.stdout.splitlines()):
                        if idx == 0 or not line.strip():
                            continue
                        parts = line.split()
                        if parts:
                            models.append(parts[0].strip())
            except Exception:
                pass
        return models

    def detect_or_ensure_vision_model(
        self,
        auto_pull: bool = True,
        pull_candidates: Tuple[str, ...] = ("qwen2.5vl:7b", "qwen2.5vl:3b"),
    ) -> Optional[str]:
        """
        ローカルOllamaから有用なVision対応モデル (qwen2.5vl 最優先 -> qwen3-vl / qwen2-vl / llava / minicpm-v 等) を優先自動検出する。
        1つも入っていない場合で auto_pull=True なら `ollama pull` を実行して導入する。
        """
        env_override = os.environ.get("VISION_MODEL_NAME", "").strip()
        if env_override:
            return env_override

        installed = self.list_installed_ollama_models()
        if self._cached_vision_model and self._cached_vision_model in installed:
            return self._cached_vision_model

        for prio in self.VISION_MODEL_PRIORITY:
            prio_low = prio.lower()
            for inst in installed:
                inst_low = inst.lower()
                if inst_low == prio_low or inst_low.startswith(prio_low + ":") or prio_low in inst_low:
                    self._cached_vision_model = inst
                    return inst

        if auto_pull:
            for target_pull in pull_candidates:
                try:
                    proc = subprocess.run(
                        ["ollama", "pull", target_pull],
                        capture_output=True,
                        text=True,
                        timeout=600.0,
                        encoding="utf-8",
                        errors="ignore",
                    )
                    if proc.returncode == 0:
                        self._cached_vision_model = target_pull
                        return target_pull
                except Exception:
                    continue
        return None

    @staticmethod
    def extract_media_frame_base64(
        media_path: str,
        media_type: Optional[str] = None,
        max_side: int = 1024,
    ) -> Optional[str]:
        """
        画像ファイル、または動画ファイルの1フレーム目（サムネイル静止画）を抽出し、
        長辺 max_side 以下にリサイズした JPEG Base64 文字列を返す。
        """
        from app.services.media_blur_service import extract_media_thumbnail_base64

        return extract_media_thumbnail_base64(
            media_path=media_path,
            media_type=media_type,
            max_side=max_side,
            jpeg_quality=88,
        )

    def _call_vision_generate(
        self,
        vision_model: str,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        temperature: float = 0.25,
        max_tokens: int = 650,
    ) -> Optional[str]:
        """
        Ollama ネイティブ /api/generate（失敗時は /api/chat フォールバック）に
        静止画 Base64 を渡して Qwen 2.5 VL 等の Vision 推論を実行する。
        """
        root_url = self._get_ollama_root_url()
        gen_url = f"{root_url}/api/generate"
        chat_url = f"{root_url}/api/chat"
        req_timeout = max(self.timeout, 90.0)
        is_qwen3 = "qwen3" in vision_model.lower()
        attempts = [True, False] if is_qwen3 else [False]

        # 1. /api/generate による JSON 構造化 Vision 推論
        for include_think_false in attempts:
            payload: Dict[str, Any] = {
                "model": vision_model,
                "system": system_prompt,
                "prompt": user_prompt,
                "images": [image_b64],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }
            if include_think_false:
                payload["think"] = False

            try:
                req = urllib.request.Request(
                    gen_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    cleaned = self.strip_think_tags(str(body.get("response") or ""))
                    if cleaned and self.extract_json_object(cleaned, default=None):
                        return cleaned
            except urllib.error.HTTPError as he:
                if include_think_false and he.code == 400:
                    continue
            except Exception:
                break

        # 2. /api/chat フォールバック
        chat_payload: Dict[str, Any] = {
            "model": vision_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt, "images": [image_b64]},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        try:
            req = urllib.request.Request(
                chat_url,
                data=json.dumps(chat_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                msg_content = str((body.get("message") or {}).get("content") or "")
                cleaned = self.strip_think_tags(msg_content)
                if cleaned:
                    return cleaned
        except Exception:
            pass

        return None

    @staticmethod
    def _clean_vision_tags(raw_tags: Any) -> List[str]:
        placeholder_or_noise = {
            "衣装や色の特徴",
            "髪色や耳・装飾の特徴",
            "ポーズや構図",
            "表情や視線",
            "場所や背景の雰囲気",
            "シチュエーションやプレイ属性",
            "日本語タグ",
            "属性タグ",
            "fyp",
            "video",
            "image",
            "mp4",
            "mov",
            "jpg",
            "png",
            "step1",
            "step2",
            "step3",
            "vault",
        }
        if isinstance(raw_tags, str):
            candidates = [t.strip().lstrip("#").strip("\"'") for t in re.split(r"[,、/\n]+", raw_tags) if t.strip()]
        elif isinstance(raw_tags, list):
            candidates = [str(t).strip().lstrip("#").strip("\"'") for t in raw_tags if str(t).strip()]
        else:
            candidates = []

        cleaned: List[str] = []
        for tag in candidates:
            low = tag.lower()
            if not tag or low in placeholder_or_noise:
                continue
            if tag.startswith("<") and tag.endswith(">"):
                continue
            if re.fullmatch(r"(?:vault_?)?\d+", low):
                continue
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:8]

    def _ensure_japanese_tags_and_memo(
        self,
        tags: List[str],
        description_memo: str,
        step: int,
        display_name: str,
    ) -> Tuple[List[str], str]:
        """万一 Vision モデルが英語主体でタグやメモを出力した場合、自然な日本語へ整形する"""
        ja_pattern = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]")
        ja_tag_count = sum(1 for t in tags if ja_pattern.search(t))
        memo_has_ja = bool(ja_pattern.search(description_memo))

        if (not tags or ja_tag_count >= max(1, len(tags) // 2)) and (not description_memo or memo_has_ja):
            return tags, description_memo

        conv_sys = (
            f"あなたはFanslyクリエイター「{display_name}」の日本人AI営業ディレクターです。\n"
            "与えられた英語の画像解析結果（tags / description_memo）を、意味を保ったまま自然で具体的な日本語の属性タグ配列と日本語売り込みメモ（2〜3文）に変換し、以下のJSONのみを出力してください:\n"
            '{"tags": ["日本語タグ1", "日本語タグ2"], "description_memo": "自然な日本語の売り込みメモ"}'
        )
        conv_user = (
            f"Step: {step}\n"
            f"Raw tags: {json.dumps(tags, ensure_ascii=False)}\n"
            f"Raw memo: {description_memo}\n"
            "Convert to natural Japanese JSON:"
        )
        converted = self.generate_json(conv_sys, conv_user, default_data={})
        conv_tags = self._clean_vision_tags(converted.get("tags"))
        conv_memo = str(converted.get("description_memo") or "").strip()

        final_tags = conv_tags if (conv_tags and any(ja_pattern.search(t) for t in conv_tags)) else tags
        final_memo = conv_memo if (conv_memo and ja_pattern.search(conv_memo)) else description_memo
        return final_tags, final_memo

    def analyze_ppv_media_with_vision(
        self,
        char_id: str,
        persona: Dict[str, Any],
        media_path: Optional[str] = None,
        filename: str = "secret_clip.mp4",
        media_type: str = "video",
        ladder_step: int = 1,
        auto_pull_model: bool = True,
        thumbnail_image_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        ローカルOllamaの Qwen 2.5 VL 等の Vision 対応モデルを用いて PPV 素材を視覚解析し、
        高精度な日本語「属性タグ (tags)」・「売り込みメモ (description_memo / pitch_note)」および
        キャラ口調の一人称英語誘い文句 (pitch_hint) を自動生成する。
        ※動画ファイルの場合は直接渡さず、必ず1フレーム目のサムネイル静止画を抽出してから Vision モデルへ渡す。
        """
        from app.services.media_blur_service import (
            extract_first_frame_image,
            image_file_to_base64,
            is_video_media,
        )

        step = max(1, min(3, int(ladder_step or 1)))
        is_video = is_video_media(media_path or filename, media_type=media_type)

        temp_thumb_created: Optional[str] = None
        effective_thumb_path: Optional[str] = None
        image_b64: Optional[str] = None

        try:
            if thumbnail_image_path and os.path.isfile(thumbnail_image_path) and os.path.getsize(thumbnail_image_path) > 0:
                effective_thumb_path = thumbnail_image_path
                image_b64 = image_file_to_base64(effective_thumb_path, max_side=1024, jpeg_quality=90)
            elif media_path and os.path.isfile(media_path) and os.path.getsize(media_path) > 0:
                if is_video:
                    # 動画は必ず1枚目フレームを静止画 JPEG として抽出してから Qwen 2.5 VL へ渡す
                    temp_thumb_created = extract_first_frame_image(
                        media_path=media_path,
                        media_type="video",
                        max_side=1024,
                        jpeg_quality=90,
                    )
                    effective_thumb_path = temp_thumb_created
                    if effective_thumb_path:
                        image_b64 = image_file_to_base64(effective_thumb_path, max_side=1024, jpeg_quality=90)
                else:
                    image_b64 = self.extract_media_frame_base64(media_path, media_type="image", max_side=1024)

            vision_model = self.detect_or_ensure_vision_model(auto_pull=auto_pull_model) if image_b64 else None

            if not image_b64 or not vision_model:
                base_draft = self.generate_ppv_draft(
                    char_id=char_id,
                    persona=persona,
                    filename=filename,
                    media_type="video" if is_video else "image",
                    ladder_step=step,
                )
                return {
                    **base_draft,
                    "used_vision": False,
                    "vision_model": vision_model,
                    "extracted_thumbnail_used": bool(is_video and effective_thumb_path),
                }

            display_name = persona.get("display_name", char_id)
            nickname = persona.get("user_nickname_default", "babe").split("/")[0].strip()
            tone = persona.get("tone_and_style", "Alluring, playful, teasing yet affectionate.")
            step_desc_map = {
                1: "Step 1（導入チラ見せ・初課金向け：気軽に開封したくなる焦らし・甘え訴求）",
                2: "Step 2（本命シークレット向け：密着感・露出度・特別感を強調した訴求）",
                3: "Step 3（超秘蔵VIP・太客向け最高峰：普段絶対に見せない禁断の二人きり共有フック）",
            }
            media_kind_ja = "動画（先頭1フレーム目のサムネイル静止画）" if is_video else "画像"

            system_prompt = (
                f"あなたはFanslyクリエイター「{display_name}」専属の優秀な日本人AI営業ディレクター兼ビジュアルアナリストです。\n"
                "添付されたPPV素材の画像を隅々まで観察し、**画像に実際に映っている視覚情報のみ**に基づいて、以下のJSONオブジェクトのみを出力してください。\n"
                "【厳守ルール】\n"
                "1. 画像に映っていない衣装・場所・ポーズ（自撮りしていないのに『自撮り』等）を推測や定型句で捏造しないこと。\n"
                "2. `tags` は、人物の外見特徴（髪色・髪型・耳や角や帽子等の装飾）、衣装（種類・色・素材・露出部位）、ポーズ・構図・アングル、表情・視線、背景・場所、プレイや雰囲気から具体的な日本語単語を6〜8個抽出すること（英単語・ローマ字・ファイル名記号は禁止）。\n"
                "3. `description_memo` は2〜3文の自然な日本語で記述すること。1文目で『どんな外見・衣装・ポーズ・表情・シチュエーションで何が視覚的な見どころか』を具体的に描写し、2〜3文目で『どんなフェチ・好みのファンに刺さり、会話のどのタイミングでどう焦らして提案すると高確率で売れるか』を実践的に書くこと（「この衣装とシチュエーションは最適なタイミングです」のような抽象的な定型文は禁止）。\n"
                f"4. `pitch_hint` のみ、クリエイター本人（一人称 I / my / me）がファン（{nickname}）へ直接語りかける誘惑的な英語DM文（1〜2文）にすること（三人称の解説文やハッシュタグは禁止）。\n"
                "{\n"
                '  "tags": ["衣装や色の特徴", "髪色や耳・装飾の特徴", "ポーズや構図", "表情や視線", "場所や背景の雰囲気", "シチュエーションやプレイ属性"],\n'
                '  "description_memo": "1文目で具体的な視覚的見どころを描写し、2〜3文目で刺さるファン層と効果的な焦らし・提案タイミングを解説した自然な日本語メモ",\n'
                '  "pitch_hint": "1-2 seductive first-person (I/my/me) English DM sentences teasing this content"\n'
                "}"
            )
            user_prompt = (
                f"クリエイター名: {display_name}\n"
                f"対象素材種別: {media_kind_ja}\n"
                f"販売階段レベル: {step_desc_map.get(step, step_desc_map[1])}\n"
                f"英語キャラ口調参考: {tone}\n"
                "添付画像を詳しく観察し、例示プレースホルダは残さず実際の視覚特徴に基づいたJSONのみを出力してください:"
            )

            raw_vision = self._call_vision_generate(
                vision_model=vision_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_b64=image_b64,
            )
            parsed = self.extract_json_object(raw_vision or "", default={}) if raw_vision else {}

            tags = self._clean_vision_tags(parsed.get("tags"))
            description_memo = str(
                parsed.get("description_memo")
                or parsed.get("pitch_note")
                or parsed.get("memo")
                or ""
            ).strip()
            pitch_hint = str(parsed.get("pitch_hint") or "").strip()
            pitch_hint = re.sub(r"\s*#[^\s]+", "", pitch_hint).strip().strip('"')

            if tags or description_memo:
                tags, description_memo = self._ensure_japanese_tags_and_memo(
                    tags=tags,
                    description_memo=description_memo,
                    step=step,
                    display_name=display_name,
                )

            fallback_draft: Optional[Dict[str, Any]] = None
            if not tags or not description_memo or not pitch_hint:
                fallback_draft = self.generate_ppv_draft(
                    char_id=char_id,
                    persona=persona,
                    filename=filename,
                    media_type="video" if is_video else "image",
                    ladder_step=step,
                )
                if not tags:
                    tags = fallback_draft["tags"]
                if not description_memo:
                    description_memo = fallback_draft["description_memo"]
                if not pitch_hint:
                    pitch_hint = fallback_draft["pitch_hint"]

            return {
                "tags": tags,
                "description_memo": description_memo,
                "pitch_note": description_memo,
                "pitch_hint": pitch_hint,
                "used_vision": bool(raw_vision and parsed),
                "vision_model": vision_model,
                "extracted_thumbnail_used": bool(is_video and (effective_thumb_path or image_b64)),
            }
        finally:
            if temp_thumb_created and os.path.exists(temp_thumb_created):
                try:
                    os.remove(temp_thumb_created)
                except Exception:
                    pass


llm_client = LLMClient()



