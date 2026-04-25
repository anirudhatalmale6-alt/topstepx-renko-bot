#!/bin/bash
export PROJECT_X_USERNAME="nirmaljitsingh2012@gmail.com"
export PROJECT_X_API_KEY="+bl7RBUffN2Gl+SJzf2pX7e4Kgt81OmBaTm3XdNEhNY="
export PROJECT_X_ACCOUNT_NAME="PRAC-V2-343477-87178021"
export PYTHONUNBUFFERED=1

cd /home/ec2-user/renko-bot

python3.12 -u renko_bot.py \
    --symbol NQ \
    --qty 1 \
    --brick-size 3.0 \
    --tg-token "8681033795:AAEMFVejp5KROBMbhmWrgZdzluwNfNlw-2U" \
    --tg-chat "-5132748957" \
    --tg-keys "Noisewonderful" \
    --ntfy-topic "nqsig-dbcced6e93549dc8"
