"""
This file contains celery tasks for contentstore views
"""
from celery.task import task
from celery.utils.log import get_task_logger
import datetime
from instructor_task.models import ReportStore
from opaque_keys.edx.keys import UsageKey
from xmodule.modulestore.django import modulestore

from .mcq import MCQBlock, RatingBlock
from .sub_api import sub_api

logger = get_task_logger(__name__)


@task()
def export_data(source_block_id_str, user_id):
    """
    Reruns a course in a new celery task.
    """
    report_date = datetime.datetime.now()

    logger.debug("Beginning data export")

    block_key = UsageKey.from_string(source_block_id_str)
    src_block = modulestore().get_item(block_key)
    course_key = src_block.scope_ids.usage_id.course_key.replace(branch=None, version_guid=None)
    course_key_str = unicode(course_key)

    # Get the root block:
    root = src_block
    while root.parent:
        root = root.get_parent()

    # Build an ordered list of blocks to include in the export - each block is a column in the CSV file
    blocks_to_include = []

    def scan_for_blocks(block):
        """ Recursively scan the course tree for blocks of interest """
        if isinstance(block, (MCQBlock, RatingBlock)):
            blocks_to_include.append(block)
        elif block.has_children:
            for child_id in block.children:
                scan_for_blocks(block.runtime.get_block(child_id))

    scan_for_blocks(root)

    # Define the header rows of our CSV:
    rows = []
    rows.append(["Student"] + [block.display_name_with_default for block in blocks_to_include])
    rows.append([""] + [block.scope_ids.block_type for block in blocks_to_include])
    rows.append([""] + [block.scope_ids.usage_id for block in blocks_to_include])

    # Load the actual student submissions for each block in blocks_to_include.
    # Note this requires one giant query per block (all student submissions for each block, one block at a time)
    student_submissions = {}  # Key is student ID, value is a list with same length as blocks_to_include
    for idx, block in enumerate(blocks_to_include, start=1):  # start=1 since first column is stuent ID
        # Get all of the most recent student submissions for this block:
        block_id = unicode(block.scope_ids.usage_id.replace(branch=None, version_guid=None))
        block_type = block.scope_ids.block_type
        for submission in sub_api.get_all_submissions(course_key_str, block_id, block_type):
            if submission.student_id not in student_submissions:
                student_submissions[submission.student_id] = [submission.student_id] + [""] * len(blocks_to_include)
            student_submissions[submission.student_id][idx] = submission.answer

    # Now change from a dict to an array ordered by student ID as we generate the remaining rows:
    for student_id in sorted(student_submissions.iterkeys()):
        rows.append(student_submissions[student_id])
        del student_submissions[student_id]

    # Generate the CSV:
    filename = u"pb-data-export-{}.csv".format(report_date.strftime("%Y-%m-%d-%H%M%S"))
    report_store = ReportStore.from_config()
    report_store.store_rows(course_key, filename, rows)

    generation_time_s = (datetime.datetime.now() - report_date).total_seconds()
    logger.debug("Done data export - took {} seconds".format(generation_time_s))

    return {
        "error": None,
        "report_filename": filename,
        "report_date": report_date.isoformat(),
        "generation_time_s": generation_time_s,
    }