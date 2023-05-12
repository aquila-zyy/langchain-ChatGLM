from abc import ABC

from langchain.llms.base import LLM
import random
import torch
import transformers
from transformers.generation.logits_process import LogitsProcessor
from transformers.generation.utils import LogitsProcessorList, StoppingCriteriaList
from typing import Optional, List, Dict, Any
from models.loader import LoaderCheckPoint
from models.extensions.callback import (Iteratorize, Stream, FixedLengthQueue)
import models.shared as shared
from models.base import (BaseAnswer,
                         AnswerResult,
                         AnswerResultStream,
                         AnswerResultQueueSentinelTokenListenerQueue)
from langchain.callbacks.manager import (
    CallbackManagerForLLMRun
)


def _streaming_response_template() -> Dict[str, Any]:
    """
    :return: 响应结构
    """
    return {
        "text": ""
    }


def _update_response(response: Dict[str, Any], stream_response: str) -> None:
    """Update response from the stream response."""
    response["text"] += stream_response


class InvalidScoreLogitsProcessor(LogitsProcessor):
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        if torch.isnan(scores).any() or torch.isinf(scores).any():
            scores.zero_()
            scores[..., 5] = 5e4
        return scores


class LLamaLLM(BaseAnswer, LLM, ABC):
    checkPoint: LoaderCheckPoint = None
    history = []
    history_len: int = 3
    max_new_tokens: int = 256
    num_beams: int = 1
    temperature: float = 0.7
    top_p: float = 0.1
    top_k: int = 10
    repetition_penalty: float = 6.1
    encoder_repetition_penalty: int = 1
    min_length: int = 0
    logits_processor: LogitsProcessorList = None
    stopping_criteria: Optional[StoppingCriteriaList] = None

    state: object = {'max_new_tokens': 50,
                     'seed': 1,
                     'temperature': 0, 'top_p': 0.1,
                     'top_k': 40, 'typical_p': 1,
                     'repetition_penalty': 1.18,
                     'encoder_repetition_penalty': 1,
                     'no_repeat_ngram_size': 0,
                     'min_length': 0,
                     'penalty_alpha': 0,
                     'num_beams': 1,
                     'length_penalty': 1,
                     'early_stopping': False, 'add_bos_token': True, 'ban_eos_token': False,
                     'truncation_length': 2048, 'custom_stopping_strings': '',
                     'cpu_memory': 0, 'auto_devices': False, 'disk': False, 'cpu': False, 'bf16': False,
                     'load_in_8bit': False, 'wbits': 'None', 'groupsize': 'None', 'model_type': 'None',
                     'pre_layer': 0, 'gpu_memory_0': 0}

    def __init__(self, checkPoint: LoaderCheckPoint = None):
        super().__init__()
        self.checkPoint = checkPoint

    @property
    def _llm_type(self) -> str:
        return "LLamaLLM"

    @property
    def _check_point(self) -> LoaderCheckPoint:
        return self.checkPoint

    def encode(self, prompt, add_special_tokens=True, add_bos_token=True, truncation_length=None):
        input_ids = self.checkPoint.tokenizer.encode(str(prompt), return_tensors='pt',
                                                     add_special_tokens=add_special_tokens)
        # This is a hack for making replies more creative.
        if not add_bos_token and input_ids[0][0] == self.checkPoint.tokenizer.bos_token_id:
            input_ids = input_ids[:, 1:]

        # Llama adds this extra token when the first character is '\n', and this
        # compromises the stopping criteria, so we just remove it
        if type(self.checkPoint.tokenizer) is transformers.LlamaTokenizer and input_ids[0][0] == 29871:
            input_ids = input_ids[:, 1:]

        # Handling truncation
        if truncation_length is not None:
            input_ids = input_ids[:, -truncation_length:]

        return input_ids.cuda()

    def decode(self, output_ids):
        reply = self.checkPoint.tokenizer.decode(output_ids, skip_special_tokens=True)
        return reply

    def generate_with_callback(self,callback=None, **kwargs):
        self.checkPoint.clear_torch_cache()
        kwargs['stopping_criteria'].append(Stream(callback_func=callback))
        with torch.no_grad():
            self.checkPoint.model.generate(**kwargs)

    def generate_with_streaming(self, **kwargs):
        return Iteratorize(self.generate_with_callback, kwargs)

    # 将历史对话数组转换为文本格式
    def history_to_text(self, query):
        formatted_history = ''
        history = self.history[-self.history_len:] if self.history_len > 0 else []
        for i, (old_query, response) in enumerate(history):
            formatted_history += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
        formatted_history += "[Round {}]\n问：{}\n答：".format(len(history), query)
        return formatted_history

    def prepare_inputs_for_generation(self,
                                      input_ids: torch.LongTensor):
        """
        预生成注意力掩码和 输入序列中每个位置的索引的张量
        # TODO 没有思路
        :return:
        """

        mask_positions = torch.zeros((1, input_ids.shape[1]), dtype=input_ids.dtype).to(self.checkPoint.model.device)

        attention_mask = self.get_masks(input_ids, input_ids.device)

        position_ids = self.get_position_ids(
            input_ids,
            device=input_ids.device,
            mask_positions=mask_positions
        )

        return input_ids, position_ids, attention_mask

    def get_position_ids(self, input_ids: torch.LongTensor, mask_positions, device):
        """
        注意力偏移量
        :param input_ids:
        :param mask_positions:
        :param device:
        :param use_gmasks:
        :return:
        """
        batch_size, seq_length = input_ids.shape
        context_lengths = [seq.tolist().index(self.checkPoint.model_config.bos_token_id) for seq in input_ids]
        position_ids = torch.arange(seq_length, dtype=torch.long, device=device).unsqueeze(0).repeat(batch_size, 1)
        for i, context_length in enumerate(context_lengths):
            position_ids[i, context_length:] = mask_positions[i]
        block_position_ids = [torch.cat((
            torch.zeros(context_length, dtype=torch.long, device=device),
            torch.arange(seq_length - context_length, dtype=torch.long, device=device) + 1
        )) for context_length in context_lengths]
        block_position_ids = torch.stack(block_position_ids, dim=0)
        position_ids = torch.stack((position_ids, block_position_ids), dim=1)
        return position_ids

    def get_masks(self, input_ids, device):
        """
        获取注意力掩码
        :param input_ids:
        :param device:
        :return:
        """
        batch_size, seq_length = input_ids.shape
        context_lengths = [seq.tolist().index(self.checkPoint.model_config.bos_token_id) for seq in input_ids]
        attention_mask = torch.ones((batch_size, seq_length, seq_length), device=device)
        attention_mask.tril_()
        for i, context_length in enumerate(context_lengths):
            attention_mask[i, :, :context_length] = 1
        attention_mask.unsqueeze_(1)
        attention_mask = (attention_mask < 0.5).bool()
        return attention_mask

    def generate_softprompt_history_tensors(self, query):
        """
        历史对话软提示
            这段代码首先定义了一个名为 history_to_text 的函数，用于将 self.history
            数组转换为所需的文本格式。然后，我们将格式化后的历史文本
            再用 self.encode 将其转换为向量表示。最后，将历史对话向量与当前输入的对话向量拼接在一起。
        :return:
        """

        # 对话内容
        # 处理历史对话
        formatted_history = self.history_to_text(query)
        # 将历史对话向量与当前对话向量拼接
        filler_input_ids = self.encode(formatted_history, add_bos_token=self.state['add_bos_token'],
                                       truncation_length=self.max_new_tokens)
        return filler_input_ids

    def _call(self,
              prompt: str,
              stop: Optional[List[str]] = None,
              run_manager: Optional[CallbackManagerForLLMRun] = None) -> str:

        if self.logits_processor is None:
            self.logits_processor = LogitsProcessorList()
        self.logits_processor.append(InvalidScoreLogitsProcessor())

        gen_kwargs = {
                      "max_new_tokens": self.max_new_tokens,
                      "num_beams": self.num_beams,
                      "top_p": self.top_p,
                      "top_k": self.top_k,
                      "repetition_penalty": self.repetition_penalty,
                      "encoder_repetition_penalty": self.encoder_repetition_penalty,
                      "min_length": self.min_length,
                      "temperature": self.temperature,
                      "logits_processor": self.logits_processor}

        input_ids = self.generate_softprompt_history_tensors(prompt)
        # input_ids, position_ids, attention_mask = self.prepare_inputs_for_generation(input_ids=filler_input_ids)

        # 对话模型prompt
        gen_kwargs.update({'inputs': input_ids})
        # 注意力掩码
        # gen_kwargs.update({'attention_mask': attention_mask})
        # gen_kwargs.update({'position_ids': position_ids})
        # 观测输出
        gen_kwargs.update({'stopping_criteria':  self.stopping_criteria})
        shared.stop_everything = False
        stopped = False
        response_template = _streaming_response_template()
        with self.generate_with_streaming(**gen_kwargs) as generator:
            last_reply_index = 0
            # Create a FixedLengthQueue with the desired stop sequence and a maximum length.
            if stop:
                queue = FixedLengthQueue(stop)

            for output in generator:
                new_tokens = len(output) - len(input_ids[0])
                reply = self.decode(output[-new_tokens:])

                new_reply = len(reply) - last_reply_index
                output_reply = reply[-new_reply:]

                if last_reply_index > 0:
                    if stop:
                        queue.add(output_reply)
                        pos = queue.contains_stop_sequence()
                        if pos != -1:
                            shared.stop_everything = True
                            stopped = True
                if new_tokens == self.max_new_tokens - 1 or stopped:
                    break
                _update_response(response_template, output_reply)
                last_reply_index = len(reply)
                if stopped:
                    break

        response = response_template['text']

        self.history = self.history + [[None, response]]
        return response

    def _generate_answer(self, prompt: str,
                         history: List[List[str]] = [],
                         streaming: bool = False,
                         generate_with_callback: AnswerResultStream = None) -> None:
        if history:
            self.history = history
        # Create the StoppingCriteriaList with the stopping strings
        self.stopping_criteria = transformers.StoppingCriteriaList()
        # 定义模型stopping_criteria 队列，在每次响应时将 torch.LongTensor, torch.FloatTensor同步到AnswerResult
        listenerQueue = AnswerResultQueueSentinelTokenListenerQueue()
        self.stopping_criteria.append(listenerQueue)
        # TODO 需要实现chat对话模块和注意力模型，目前_call为langchain的LLM拓展的api，默认为无提示词模式，如果需要操作注意力模型，可以参考chat_glm的实现
        response = self._call(prompt=prompt, stop=['\n###'])
        answer_result = AnswerResult()
        answer_result.history = self.history
        if listenerQueue.listenerQueue.__len__() > 0:
            answer_result.listenerToken = listenerQueue.listenerQueue.pop()
        answer_result.llm_output = {"answer": response}
        generate_with_callback(answer_result)
