# data/plugins/astrbot_plugin_mimo_clonetts/main.py
import random
import base64
import aiohttp
from pathlib import Path
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Record, Plain
from astrbot.api import logger, AstrBotConfig

@register("astrbot_plugin_mimo_clonetts", "ayakadaisuki", "小米 MIMO 语音克隆", "1.0.0")
class MimoCloneTTSPlugin(Star):
    """小米 MIMO 语音克隆 TTS 插件（多音色支持）"""
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._audio_b64_cache = None
        self._init_error = None
        # 🔑 音色目录固定在插件内部，升级不丢失
        self.voices_dir = Path(__file__).parent / "voices"
        
    async def initialize(self):
        self.voices_dir.mkdir(exist_ok=True)
        voices = self._list_voices()
        if not voices:
            self._init_error = "voices/ 目录为空，请放入至少一个 .wav/.mp3 参考音频"
            logger.warning(f"⚠️ {self._init_error}")
        else:
            logger.info(f"🎙️ 发现 {len(voices)} 个音色: {', '.join(voices)}")
        logger.info("🎙️ MIMO 语音克隆插件已加载（多音色支持）")
        
    async def terminate(self):
        self._audio_b64_cache = None
        
    def _list_voices(self) -> list:
        """扫描 voices/ 目录下的合法音频文件"""
        if not self.voices_dir.exists():
            return []
        return [f.name for f in self.voices_dir.iterdir() if f.suffix.lower() in ('.wav', '.mp3')]
        
    async def _preload_audio(self) -> str:
        """懒加载：首次合成时编码当前选中的音色"""
        if self._audio_b64_cache:
            return self._audio_b64_cache
            
        voice_name = self.config.get("selected_voice", "default.wav")
        voice_path = self.voices_dir / voice_name
        
        if not voice_path.exists():
            available = self._list_voices()
            raise FileNotFoundError(f"音色文件不存在: {voice_name}。可用: {available or '无'}")
            
        with open(voice_path, "rb") as f:
            audio_bytes = f.read()
            
        # 严格单行 Base64，去除所有隐藏空白/换行
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8").strip()
        mime = "audio/wav" if voice_path.suffix.lower() == ".wav" else "audio/mpeg"
        self._audio_b64_cache = f"data:{mime};base64,{audio_b64}"
        
        # 🔍 调试日志：打印实际发送给 API 的前 60 个字符
        logger.info(f"🔍 [DEBUG] audio.voice 实际前缀: {self._audio_b64_cache[:60]}...")
        logger.info(f"✅ 已加载音色: {voice_name} ({len(audio_bytes)} 字节)")
        return self._audio_b64_cache
        
    def _should_skip(self, text: str) -> bool:
        min_len = self.config.get("min_text_len", 5)
        max_len = self.config.get("max_text_len", 200)
        if len(text) < min_len or len(text) > max_len:
            return True
        prob = self.config.get("tts_probability", 80)
        if random.randint(0, 99) >= prob:
            return True
        return False
        
    async def _synthesize(self, text: str, style: str = "") -> bytes:
        audio_b64 = await self._preload_audio()
        messages = [{"role": "assistant", "content": text}]
        if style.strip():
            messages.insert(0, {"role": "user", "content": style})
            
        payload = {
            "model": self.config.get("model", "mimo-v2.5-tts-voiceclone"),
            "messages": messages,
            "audio": {"voice": audio_b64}
        }
        
        headers = {
            "api-key": self.config.get("api_key"),
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(
                self.config.get("endpoint", "https://api.xiaomimimo.com/v1/chat/completions"),
                headers=headers,
                json=payload
            ) as resp:
                if resp.status != 200:
                    error = await resp.json()
                    raise RuntimeError(f"MIMO API {resp.status}: {error}")
                result = await resp.json()
                return base64.b64decode(result["choices"][0]["message"]["audio"]["data"])
                
    # ========== 被动模式 ==========
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        if self._init_error:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
            
        text_parts = [c.text for c in result.chain if isinstance(c, Plain)]
        if not text_parts:
            return
        text = "".join(text_parts)
        
        if self._should_skip(text):
            return
            
        if not self.config.get("exclusive_mode", False):
            if any(isinstance(c, Record) for c in result.chain):
                return
                
        try:
            style = self.config.get("style_prompt", "")
            wav_bytes = await self._synthesize(text, style)
            
            from astrbot.core.utils.io import get_astrbot_data_path
            tmp_dir = Path(get_astrbot_data_path()) / "tmp" / "mimo"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / f"{hash(text)}.wav"
            with open(tmp_path, "wb") as f:
                f.write(wav_bytes)
                
            result.chain = [Record.fromFileSystem(str(tmp_path))]
            logger.info(f"🎙️ 语音合成成功: {text[:30]}...")
            
            if self.config.get("exclusive_mode", False):
                event.stop_propagation()
        except Exception as e:
            logger.error(f"❌ 语音合成失败: {e}")
            
    # ========== LLM 工具 ==========
    @filter.llm_tool(
        name="mimo_tts",
        description="使用音色克隆将文本转为语音。参数: text(要合成的文本), style(可选: 风格指令)"
    )
    async def mimo_tts_tool(self, event: AstrMessageEvent, text: str, style: str = ""):
        if self._init_error:
            yield event.plain_result(f"语音功能未就绪: {self._init_error}")
            return
        try:
            wav_bytes = await self._synthesize(text, style or self.config.get("style_prompt", ""))
            from astrbot.core.utils.io import get_astrbot_data_path
            tmp_dir = Path(get_astrbot_data_path()) / "tmp" / "mimo"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / f"tool_{hash(text)}.wav"
            with open(tmp_path, "wb") as f:
                f.write(wav_bytes)
            yield event.chain_result([Record.fromFileSystem(str(tmp_path))])
        except Exception as e:
            logger.error(f"❌ [工具] 语音合成失败: {e}")
            yield event.plain_result(f"语音生成失败: {e}")
            
    # ========== 管理指令 ==========
    @filter.command("mimo_voices")
    async def list_voices_cmd(self, event: AstrMessageEvent):
        """查看可用音色列表"""
        voices = self._list_voices()
        current = self.config.get("selected_voice", "default.wav")
        if not voices:
            yield event.plain_result("📂 voices/ 目录为空，请放入 .wav/.mp3 文件")
            return
        lines = ["🎙️ 可用音色列表:"]
        for v in voices:
            marker = " 👈 当前" if v == current else ""
            lines.append(f"• {v}{marker}")
        lines.append("\n💡 切换指令: /mimo_set_voice <文件名>")
        yield event.plain_result("\n".join(lines))
        
    @filter.command("mimo_set_voice")
    async def set_voice_cmd(self, event: AstrMessageEvent, voice_name: str):
        """切换当前音色"""
        voice_path = self.voices_dir / voice_name
        if not voice_path.exists():
            yield event.plain_result(f"❌ 找不到音色: {voice_name}\n使用 /mimo_voices 查看列表")
            return
        self.config["selected_voice"] = voice_name
        self._audio_b64_cache = None  # 清除缓存，下次合成自动重载
        yield event.plain_result(f"✅ 已切换音色: {voice_name}\n下次合成将生效")
        
    @filter.command("mimo_reload")
    async def reload_cmd(self, event: AstrMessageEvent):
        """手动重载当前音色缓存"""
        self._audio_b64_cache = None
        try:
            await self._preload_audio()
            self._init_error = None
            yield event.plain_result("✅ 音色缓存已重载")
        except Exception as e:
            self._init_error = str(e)
            yield event.plain_result(f"❌ 重载失败: {e}")