"""Optional Inspect solver for AstaBench ScholarQA-CS2 (requires inspect_ai)."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import agent
from inspect_ai.model import ModelOutput
from inspect_ai.solver import solver
from evaluation.public_research.scholarqa import execute


@solver
def astrion_scholarqa(max_steps=10, workers=3, worker_steps=6, budget_chars=12000,
                      output_dir='research_data/public_benchmarks/results'):
    async def solve(state, generate):
        del generate
        agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parents[2]))
        key=agent.first_nonempty(os.getenv('AGENT_API_KEY'),os.getenv('DEEPSEEK_API_KEY'),os.getenv('OPENAI_API_KEY'))
        if not key: raise ValueError('model API key not configured')
        url=os.getenv('AGENT_BASE_URL') or 'https://api.deepseek.com'
        model=os.getenv('AGENT_MODEL') or 'deepseek-chat'
        path, report=await asyncio.to_thread(execute,state.input_text,lambda:agent.ChatLLM(key,url,model),output_dir,
                       steps=max_steps,workers=workers,worker_steps=worker_steps,budget_chars=budget_chars)
        result=report['result'].get('result') or {}
        if not result.get('answer'): raise RuntimeError('Astrion produced no answer; see '+str(path))
        state.output=ModelOutput.from_content(model=model,content=result['answer'])
        return state
    return solve
