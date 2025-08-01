"""Dialogue generation module.

文件结构:

```
DialogueEngine -> 负责根据人格与记忆生成回复
```

The engine receives perceived emotion, user identity and textual input from
``IntelligentCore``. It consults the personality vector and semantic memory to
compose a reply, then maps mood and touch into actions and expressions.
该模块从 ``IntelligentCore`` 获取情绪、身份和文本信息，结合人格向量与
语义记忆生成回复，并给出相应的动作和表情。
"""

# Growth stages
# 成长阶段说明:
# 1. sprout   - 萌芽期 (0~3 天)
# 2. enlighten - 启蒙期 (3~10 天)
# 3. resonate - 共鸣期 (10~30 天)
# 4. awaken   - 觉醒期 (30 天以上)

from dataclasses import dataclass
from typing import Optional
import logging

from . import global_state

from .personality_engine import PersonalityEngine
from .semantic_memory import SemanticMemory
from .service_api import call_llm, call_tts
from .constants import (
    DEFAULT_GROWTH_STAGE,
    LOG_LEVEL,
    FACE_ANIMATION_MAP,
    ACTION_MAP,
    STAGE_LLM_PROMPTS,
    STAGE_LLM_PROMPTS_CN,
    OCEAN_LLM_PROMPTS,
    OCEAN_LLM_PROMPTS_CN,
    TOUCH_ZONE_PROMPTS,
)
from .prompt_fusion import PromptFusionEngine, create_prompt_factors

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)



@dataclass
class DialogueResponse:
    """Structured output of the dialogue engine.

    对话引擎生成的结构化回应，包括文本、音频、动作和表情。
    """

    text: str
    audio: str
    action: list[str]
    expression: str

    def as_dict(self) -> dict:
        """Convert to plain dictionary.

        转换为普通字典以便序列化输出。
        """
        return {
            "text": self.text,
            "audio": self.audio,
            "action": self.action,
            "expression": self.expression,
        }


class DialogueEngine:
    """Dialogue system that grows with interactions.

    通过互动逐步成长的对话系统。
    """

    def __init__(
        self,
        personality: Optional[PersonalityEngine] = None,
        memory: Optional[SemanticMemory] = None,
        llm_url: str | None = None,
        tts_url: str | None = None,
    ) -> None:
        """Initialize dialogue engine with personality and memory modules.

        使用人格和记忆模块初始化对话引擎。

        Parameters
        ----------
        personality: PersonalityEngine, optional
            Custom personality engine. 默认为 :class:`PersonalityEngine` 实例。
        memory: SemanticMemory, optional
            Memory storage module. 默认为 :class:`SemanticMemory`。
        llm_url: str | None, optional
            Endpoint for remote large language model service. If ``None``,
            simple local templates are used.
        tts_url: str | None, optional
            Endpoint for text-to-speech service. ``None`` disables TTS.
        """
        self.personality = personality or PersonalityEngine()
        self.memory = memory or SemanticMemory()
        self.llm_url = llm_url
        self.tts_url = tts_url
        self.stage = global_state.get_growth_stage()  # 初始成长阶段
        if not self.stage:
            self.stage = DEFAULT_GROWTH_STAGE
        self.prompt_fusion = PromptFusionEngine()
        logger.debug("Dialogue engine initialized at stage %s", self.stage)

    def _infer_behavior_tag(self, text: str, mood: str) -> str | None:
        """Infer behavior tag from text and mood."""

        text_l = text.lower()
        if mood == "angry" or "bad" in text_l:
            return "criticism"
        if mood in ("happy", "excited") or "thanks" in text_l:
            return "praise"
        if "joke" in text_l or "haha" in text_l:
            return "joke"
        if mood == "sad":
            return "support"
        return None

    def generate_response(
        self,
        user_text: str,
        mood_tag: str = "neutral",
        user_id: str = "unknown",
        touched: bool = False,
        touch_zone: int | None = None,
    ) -> DialogueResponse:
        """Generate an AI reply based on memory and personality.

        根据记忆和人格状态生成回答。

        Parameters
        ----------
        user_text: str
            Incoming user message.
        mood_tag: str, optional
            Emotion label influencing personality update. Defaults to
            ``"neutral"``.
        touched: bool, optional
            Whether the robot was touched during this interaction.
        touch_zone: int | None, optional
            Identifier for the touch sensor zone. ``None`` means no touch
            detected.

        Returns
        -------
        DialogueResponse
            Reply with text, audio URL, action list and expression name. All
            fields are guaranteed to be non-empty.
        """
        logger.info(
            "Generating response for user %s with mood %s", user_id, mood_tag
        )
        # 1. personality update
        # 根据情绪标签与触摸行为更新人格向量
        self.personality.update(mood_tag)
        if touched:
            self.personality.update("touch")
        behavior_tag = self._infer_behavior_tag(user_text, mood_tag)
        if behavior_tag:
            self.personality.update(behavior_tag)
        # 2. determine growth stage using global metrics
        # 根据全局统计信息判断成长阶段
        self.stage = global_state.get_growth_stage()

        style = self.personality.get_personality_style()
        personality_summary = self.personality.get_personality_summary()
        dominant_traits = self.personality.get_dominant_traits()
        
        past = self.memory.query_memory(user_text, user_id=user_id)
        logger.debug("Retrieved %d past records", len(past))
        
        # 优化记忆摘要生成
        if past:
            # 过滤掉空回复和无效回复
            valid_responses = []
            for p in past:
                response = p["ai_response"].strip()
                user_text = p["user_text"].strip()
                # 确保回复不为空且有意义
                if response and len(response) > 2 and not response.startswith("[") and user_text:
                    valid_responses.append({
                        "user": user_text,
                        "ai": response,
                        "mood": p.get("mood_tag", "neutral")
                    })
            
            if valid_responses:
                # 选择最相关的回复，构建更丰富的记忆摘要
                best_match = valid_responses[0]
                past_summary = f"用户说'{best_match['user']}'时，我回复'{best_match['ai']}'"
                
                # 如果有多个相关记忆，添加更多上下文
                if len(valid_responses) > 1:
                    second_match = valid_responses[1]
                    past_summary += f"。另外，当用户说'{second_match['user']}'时，我回复'{second_match['ai']}'"
                
                # 添加情绪信息
                mood_counts = {}
                for resp in valid_responses:
                    mood = resp.get("mood", "neutral")
                    mood_counts[mood] = mood_counts.get(mood, 0) + 1
                
                if mood_counts:
                    dominant_mood = max(mood_counts.items(), key=lambda x: x[1])[0]
                    if dominant_mood != "neutral":
                        past_summary += f"。这些对话中用户情绪主要是{dominant_mood}"
            else:
                past_summary = ""
        else:
            past_summary = ""

        # 3. generate base response
        if self.stage == "sprout":
            base_resp = "呀呀" if user_text else "咿呀"
        elif self.stage == "enlighten":
            base_resp = "你好" if user_text else "你好"
        elif self.stage == "resonate":
            base_resp = f"[{style}] {user_text}? 我在听哦"
        else:  # awaken
            base_resp = f"[{style}] Based on our chats: {past_summary} | {user_text}"

        # 使用提示词融合算法构建优化提示词
        stage_info = {
            "prompt": f"{STAGE_LLM_PROMPTS.get(self.stage, '')} {STAGE_LLM_PROMPTS_CN.get(self.stage, '')}"
        }
        
        personality_info = {
            "traits": f"{', '.join(OCEAN_LLM_PROMPTS.values())} ({', '.join(OCEAN_LLM_PROMPTS_CN.values())})",
            "style": style,
            "summary": personality_summary,
            "dominant_traits": dominant_traits
        }
        
        emotion_info = {
            "emotion": mood_tag
        }
        
        touch_info = {
            "content": TOUCH_ZONE_PROMPTS.get(touch_zone, "") if touched else ""
        }
        
        memory_info = {
            "summary": past_summary,
            "count": len(past)
        }
        
        # 创建提示词因子
        factors = create_prompt_factors(
            stage_info=stage_info,
            personality_info=personality_info,
            emotion_info=emotion_info,
            touch_info=touch_info,
            memory_info=memory_info,
            user_input=user_text
        )
        
        # 使用融合算法生成优化提示词
        prompt = self.prompt_fusion.fuse_prompts(factors)
        
        # 打印详细的提示词信息
        logger.info("=== LLM Prompt Fusion ===")
        logger.info(f"Growth Stage: {self.stage}")
        logger.info(f"Personality Style: {style}")
        logger.info(f"Personality Summary: {personality_summary}")
        logger.info(f"Dominant Traits: {dominant_traits}")
        logger.info(f"Touch Zone: {touch_zone if touched else 'None'}")
        logger.info(f"User Emotion: {mood_tag}")
        logger.info(f"Memory Records: {len(past)}")
        logger.info(f"Memory Summary: {past_summary[:100]}...")
        logger.info(f"User Input: {user_text}")
        logger.info(f"Prompt Factors Count: {len(factors)}")
        logger.info("--- Fused Prompt ---")
        logger.info(prompt)
        logger.info("=== End Prompt Fusion ===")

        response = base_resp
        if self.llm_url:
            try:
                logger.info("=" * 80)
                logger.info("🤖 LLM调用详细信息")
                logger.info("=" * 80)
                logger.info(f"📡 服务类型: {self.llm_url}")
                logger.info(f"🎯 用户输入: {user_text}")
                logger.info(f"😊 情绪状态: {mood_tag}")
                logger.info(f"👤 用户ID: {user_id}")
                logger.info(f"🤗 触摸状态: {touched}")
                logger.info(f"📍 触摸区域: {touch_zone if touched else 'None'}")
                logger.info(f"🌱 成长阶段: {self.stage}")
                logger.info(f"🎭 人格风格: {style}")
                logger.info(f"📝 人格摘要: {personality_summary}")
                logger.info(f"⭐ 主导特质: {', '.join(dominant_traits)}")
                logger.info(f"💾 记忆记录数: {len(past)}")
                logger.info(f"📚 记忆摘要: {past_summary[:100]}...")
                logger.info("-" * 80)
                logger.info("🔧 优化后的提示词:")
                logger.info(prompt)
                logger.info("-" * 80)
                
                # 如果是百炼服务，使用异步调用
                if self.llm_url == "qwen" or self.llm_url == "qwen-service":
                    import asyncio
                    from .service_api import async_call_llm
                    logger.info("🚀 调用百炼API...")
                    # 检查是否已经在事件循环中
                    try:
                        loop = asyncio.get_running_loop()
                        # 如果已经在事件循环中，使用create_task
                        task = loop.create_task(async_call_llm(prompt, self.llm_url))
                        llm_out = task.result()
                    except RuntimeError:
                        # 如果没有运行的事件循环，使用run
                        llm_out = asyncio.run(async_call_llm(prompt, self.llm_url))
                elif self.llm_url == "doubao":
                    # 豆包服务需要系统提示词
                    system_prompt = f"""你是一个智能机器人助手，具有以下特点：
1. 成长阶段：{self.stage}
2. 人格特质：{personality_summary}
3. 主导特质：{', '.join(dominant_traits)}
4. 当前风格：{style}

请根据用户输入和上下文生成自然、友好的回复。"""
                    logger.info("🚀 调用豆包API...")
                    logger.info(f"📋 系统提示词: {system_prompt}")
                    from .doubao_service import get_doubao_service
                    service = get_doubao_service()
                    llm_out = service._call_sync(prompt, system_prompt=system_prompt, history=None)
                else:
                    logger.info(f"🚀 调用其他API: {self.llm_url}")
                    llm_out = call_llm(prompt, self.llm_url)
                
                logger.info(f"📤 LLM原始输出: {llm_out}")
                
                if llm_out and llm_out.strip():
                    response = llm_out.strip()
                    logger.info(f"✅ LLM响应成功: {response[:200]}...")
                else:
                    logger.warning("⚠️ LLM返回空响应，使用基础回复")
            except Exception as e:
                logger.error(f"❌ LLM调用失败: {e}, 使用基础回复")
                response = base_resp
        else:
            logger.warning("⚠️ 未配置LLM URL，使用基础回复")

        # 4. store this interaction in memory
        self.memory.add_memory(
            user_text,
            response,
            mood_tag,
            user_id,
            touched,
            touch_zone,
        )
        logger.debug("Memory stored for user %s", user_id)

        # 5. derive action and expression from mood
        mood_key = mood_tag if mood_tag in FACE_ANIMATION_MAP else "happy"
        face_anim = FACE_ANIMATION_MAP.get(mood_key, ("E000:平静表情", "自然状态、轻微呼吸动作"))
        expression = f"{face_anim[0]} | {face_anim[1]}".strip()

        # 获取动作列表，格式：动作编号+动作+角度+说明
        action_raw = ACTION_MAP.get(mood_key, "A000:breathing|轻微呼吸动作")
        action_parts = action_raw.split("|")
        action = []
        
        # 将动作字符串解析为结构化动作列表
        for i in range(0, len(action_parts), 2):
            if i + 1 < len(action_parts):
                action_code = action_parts[i].strip()
                action_desc = action_parts[i + 1].strip()
                action.append(f"{action_code}|{action_desc}")
            else:
                action.append(action_parts[i].strip())
        
        if touched:
            # 添加触摸动作
            touch_actions = {
                0: "A100:hug|拥抱动作",
                1: "A101:pat|轻拍动作", 
                2: "A102:tickle|挠痒动作"
            }
            touch_action = touch_actions.get(touch_zone, "A100:hug|拥抱动作")
            action.append(touch_action)
        
        logger.info("🎭 表情输出: %s", expression)
        logger.info("🤸 动作输出: %s", action)

        # TTS generates an audio URL when service is provided
        audio_url = call_tts(response, self.tts_url) if self.tts_url else ""
        if not audio_url:
            audio_url = "n/a"  # 保证音频字段不为空

        logger.info("Generated response: %s", response)

        return DialogueResponse(
            text=response,
            audio=audio_url,
            action=action,
            expression=expression,
        )