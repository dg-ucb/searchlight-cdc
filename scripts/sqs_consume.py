#!/usr/bin/env python3

"""
Copyright ©2026. The Regents of the University of California (Regents). All Rights Reserved.

Permission to use, copy, modify, and distribute this software and its documentation
for educational, research, and not-for-profit purposes, without fee and without a
signed licensing agreement, is hereby granted, provided that the above copyright
notice, this paragraph and the following two paragraphs appear in all copies,
modifications, and distributions.

Contact The Office of Technology Licensing, UC Berkeley, 2150 Shattuck Avenue,
Suite 510, Berkeley, CA 94720-1620, (510) 643-7201, otl@berkeley.edu,
http://ipira.berkeley.edu/industry-info for commercial licensing opportunities.

IN NO EVENT SHALL REGENTS BE LIABLE TO ANY PARTY FOR DIRECT, INDIRECT, SPECIAL,
INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS, ARISING OUT OF
THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF REGENTS HAS BEEN ADVISED
OF THE POSSIBILITY OF SUCH DAMAGE.

REGENTS SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE
SOFTWARE AND ACCOMPANYING DOCUMENTATION, IF ANY, PROVIDED HEREUNDER IS PROVIDED
"AS IS". REGENTS HAS NO OBLIGATION TO PROVIDE MAINTENANCE, SUPPORT, UPDATES,
ENHANCEMENTS, OR MODIFICATIONS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sqs_consume_lib import HandlerConfig, run_consume

"""Non-destructive SQS consumer for local QA testing.

Polls the queue, invokes handler.lambda_handler per message.
Messages are never deleted from the queue.

Usage:
  python scripts/sqs_consume.py
  python scripts/sqs_consume.py --dry-run --max-messages 5
  python scripts/sqs_consume.py --max-messages 100 --save-dir data/sqs_live
"""


def apply_handler_env(fe: dict) -> None:
    os.environ['LOCAL_DEV'] = str(fe.get('LOCAL_DEV', 'true')).lower()
    for key in (
        'DB_HOST',
        'DB_PORT',
        'DB_USER',
        'DB_PASSWORD',
        'DB_NAME',
        'RDS_SCHEMA_BOA_APP_RDS_DATA',
        'HANDLER_VERSION',
        'COMMENTS_TABLE',
        'APPT_COMMENT_FTS_TABLE',
        'NOTES_TABLE',
        'TOPICS_TABLE',
        'AUTHORS_TABLE',
        'FTS_TABLE',
        'CDC_LOG_TABLE',
        'PENDING_TOPICS_TABLE',
    ):
        if key in fe and fe[key] is not None:
            os.environ[key] = str(fe[key])
    for key in (
        'NOTES_DELTA_TABLE',
        'NOTES_NIGHTLY_TABLE',
        'TOPICS_DELTA_TABLE',
        'TOPICS_NIGHTLY_TABLE',
        'FTS_DELTA_TABLE',
        'FTS_NIGHTLY_TABLE',
    ):
        os.environ.pop(key, None)
    os.environ.setdefault('RDS_SCHEMA_BOA_APP_RDS_DATA', 'boa_app_rds_data')
    os.environ.setdefault('HANDLER_VERSION', 'direct-v1')


CONFIG = HandlerConfig(
    handler_module='handler',
    handler_label='direct',
    default_env_key='CDCHandler',
    default_schema='boa_app_rds_data',
    apply_handler_env=apply_handler_env,
    doc=__doc__,
)

if __name__ == '__main__':
    raise SystemExit(run_consume(CONFIG))
