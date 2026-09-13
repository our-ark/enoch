from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from enoch.app.core import EnochApplication
from enoch.documents import MAX_DOCUMENT_BYTES, document_context, pdf_preview, retain_document
from enoch.channel import ChannelAttachmentError
from enoch.identity import load_identity
from enoch.providers import Attachment, ChatEvent, ChatProviderError
from enoch.tasks.queue import task_queue_status
from tests.test_enoch_providers import _Chat, _Runtime


def write_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 72 720 Td (Research evidence from the uploaded paper.) Tj ET')
    page[NameObject('/Contents')] = stream
    writer.write(path)


class DocumentTests(unittest.TestCase):
    def attachment(self, id='F123', filename='paper.pdf'):
        return Attachment('document', id, 'application/pdf', filename)

    def test_retained_pdf_has_private_stable_path_and_real_preview(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            provider = Mock()
            provider.download_attachment.side_effect = lambda a, p, **kw: write_pdf(p)
            attachment = self.attachment(filename='../../outside.pdf')
            path = retain_document(provider, attachment, root, channel='slack', conversation_id='D123')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(path.is_relative_to(root.resolve()))
            self.assertEqual(retain_document(provider, attachment, root, channel='slack', conversation_id='D123'), path)
            provider.download_attachment.assert_called_once()
            preview = pdf_preview(path)
            self.assertEqual(preview['pages'], 1)
            self.assertFalse(preview['truncated'])
            self.assertIn('Research evidence', preview['text'])
            other = retain_document(provider, attachment, root, channel='slack', conversation_id='D999')
            self.assertNotEqual(other, path)

    def test_invalid_or_oversized_downloads_are_removed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for data in (b'<html>sign in</html>', b'', b'%PDF-' + b'x' * MAX_DOCUMENT_BYTES):
                provider = Mock()
                provider.download_attachment.side_effect = lambda a, p, **kw: p.write_bytes(data)
                with self.assertRaises(ChannelAttachmentError):
                    retain_document(provider, self.attachment(), root, channel='slack', conversation_id='D123')
                self.assertFalse(list(root.rglob('*.pdf')))
            provider.reset_mock()
            with self.assertRaises(ChannelAttachmentError):
                retain_document(provider, replace(self.attachment(), size=MAX_DOCUMENT_BYTES + 1), root,
                                channel='slack', conversation_id='D123')
            provider.download_attachment.assert_not_called()

    def test_partial_failure_reports_each_file_without_losing_success(self):
        with TemporaryDirectory() as temp:
            provider = Mock()
            def download(a, p, **kw):
                if a.file_id == 'F456':
                    raise ChatProviderError('missing_scope')
                write_pdf(p)
            provider.download_attachment.side_effect = download
            context = document_context(provider, [self.attachment(), self.attachment('F456', 'second.pdf')],
                                       Path(temp), channel='slack', conversation_id='D123')
            self.assertIn('Research evidence', context)
            self.assertIn('download failed', context)
            self.assertIn('missing_scope', context)

    def test_file_only_two_pdfs_reach_runtime_and_survive_for_later_tasks(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat, runtime = _Chat(), _Runtime()
            chat.download_attachment = Mock(side_effect=lambda a, p, **kw: write_pdf(p))
            app = EnochApplication(load_identity(), root, chat, runtime=runtime)
            event = ChatEvent('file-event', 'room-1', '', 'message-1', attachments=(
                self.attachment(), self.attachment('F456', 'second.pdf')))
            with patch('enoch.app.core.log_conversation_turn'), patch('enoch.app.core.ensure_long_term_memory'):
                app.handle_event(event)
                app.handle_event(event)
            self.assertEqual(len(runtime.messages), 1)
            self.assertEqual(chat.download_attachment.call_count, 2)
            prompt = runtime.messages[0][1]
            self.assertIn('paper.pdf', prompt)
            self.assertIn('second.pdf', prompt)
            self.assertIn('Research evidence', prompt)
            self.assertEqual(len(list(root.rglob('*.pdf'))), 2)

    def test_command_caption_keeps_attachment_context_in_durable_queue(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            chat.download_attachment = Mock(side_effect=lambda a, p, **kw: write_pdf(p))
            app = EnochApplication(load_identity(), root, chat, runtime=_Runtime())
            event = ChatEvent('file-task', 'room-1', '/task add Compare the paper', 'message-1',
                              attachments=(self.attachment(),))
            with patch('enoch.app.core.log_conversation_turn'), patch('enoch.app.core.ensure_long_term_memory'):
                app.handle_event(event)
            job, = task_queue_status(root).pending
            self.assertIn('Compare the paper', job.text)
            self.assertIn('local_path', job.text)
            self.assertIn('Research evidence', job.text)

    def test_preview_extraction_failure_is_explicit_and_preserves_file(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.pdf'
            path.write_bytes(b'%PDF-invalid')
            self.assertIn('unavailable', pdf_preview(path)['status'])
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
