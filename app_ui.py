# -*- coding: utf-8 -*-
"""星辰科技知识库客服网页。

把输入发给本机 api_server，并按固定 thread_id 保留多轮上下文。

依赖：
    pip install streamlit requests

运行：
    streamlit run app_ui.py
"""

from __future__ import annotations

import os

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/") + "/chat"
THREAD_ID = "ui-test"
TIMEOUT_SECONDS = 180

st.set_page_config(page_title="星辰科技知识库客服")
st.title("星辰科技知识库客服")

if "messages" not in st.session_state:
    st.session_state.messages = []


def ask(question: str) -> str:
    response = requests.post(
        API_URL,
        json={"question": question, "thread_id": THREAD_ID},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    answer = str(data.get("answer") or "").strip()
    return answer or "这次没有生成回答，请换个说法再试一次。"


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("请输入问题")
if not question:
    st.stop()

st.session_state.messages.append({"role": "user", "content": question})
with st.chat_message("user"):
    st.markdown(question)

with st.chat_message("assistant"):
    try:
        with st.spinner("客服正在思考..."):
            answer = ask(question)
    except requests.RequestException as exc:
        answer = f"请求客服接口失败：{exc}"
    st.markdown(answer)

st.session_state.messages.append({"role": "assistant", "content": answer})
