docker build . -t cr.local.fzxu.me:5000/llm_gateway:$1 \
&& docker push cr.local.fzxu.me:5000/llm_gateway:$1
