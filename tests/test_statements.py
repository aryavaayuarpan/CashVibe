from __future__ import annotations
import hashlib
import io
from datetime import date
import pytest
from cashvibe.statement_parser import parse_text,parse_pdf,csv_rows,cents,identify_app,identify_platform,enrich
from cashvibe.agent import Agent
from cashvibe.personal_spendings import export_statement_csv


def row(**kw):
    return enrich(dict(occurred_on='2026-11-15',description='PhonePe Swiggy lunch',amount_cents=25000,kind='expense',classification='expense',**kw))


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def pdf_data(password=None):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.pdfencrypt import StandardEncryption
    out=io.BytesIO();c=Canvas(out,encrypt=StandardEncryption(password) if password else None)
    c.setFont('Courier',9)
    lines=[f"{'Date':<14}{'Description':<35}{'Debit':>12}{'Credit':>12}{'Balance':>12}",
           f"{'15/11/2026':<14}{'PhonePe Swiggy lunch':<35}{'250.00':>12}{'':>12}{'750.00':>12}",
           f"{'16/11/2026':<14}{'Google Pay refund':<35}{'':>12}{'100.00':>12}{'850.00':>12}"]
    for i,line in enumerate(lines):c.drawString(20,780-i*20,line)
    c.save();return out.getvalue()


def test_exact_amounts_and_app_attribution():
    assert cents('1,234.56')==123456
    for invalid in ['NaN','Infinity','1.234','-2']:
        with pytest.raises(ValueError):cents(invalid)
    assert identify_app('UPI/123456789012/me@ybl')=='UPI (app unknown)'
    assert identify_app('Google Pay Swiggy')=='Google Pay'
    assert identify_platform('Amazon Pay')=='Other / unknown'
    assert identify_platform('PhonePe Swiggy')=='Swiggy'


def test_pdf_columns_password_and_directions():
    for password in [None,'sample-password']:
        result=parse_pdf(pdf_data(password),password or '')
        assert [(r['amount_cents'],r['kind']) for r in result['rows']]==[(25000,'expense'),(10000,'income')]
        assert result['rows'][0]['platform']=='Swiggy'
    with pytest.raises(ValueError,match='password'):parse_pdf(pdf_data('secret'),'wrong')


def test_ambiguous_pdf_never_invents_direction():
    rows,warnings=parse_text('Date         Description                Amount\n15/11/2026   UPI purchase                250.00')
    assert rows[0]['kind']=='Review required'


def test_csv_mapping_and_failed_rows_reported():
    rows,warnings,_=csv_rows(b'When,What,Paid,Got\n15/11/2026,Paytm Zomato,150.50,\n16/11/2026,Refund,,20.00\nwrong,Bad,30,\n',{'date':'When','description':'What','debit':'Paid','credit':'Got'})
    assert [(r['amount_cents'],r['kind']) for r in rows]==[(15050,'expense'),(2000,'income')]
    assert warnings and rows[0]['payment_app']=='Paytm'
    assert b"'=HYPERLINK" in export_statement_csv([dict(row(),description='=HYPERLINK("bad")')])


def test_storage_private_atomic_deduplicated_and_summary(store,accounts):
    alice,bob,_=accounts
    a,b=alice['id'],bob['id']
    result=store.save_statement_import(a,'November',digest('first'),[row(),dict(row(),description='Salary',amount_cents=500000,kind='income',classification='income'),dict(row(),description='Move to savings',amount_cents=100000,classification='transfer')])
    assert result['saved']==3
    summary=store.statement_summary(a,month='2026-11',day='2026-11-15')
    assert (summary['money_out_cents'],summary['money_in_cents'],summary['spending_cents'])==(125000,500000,25000)
    assert store.list_statement_entries(b)==[]
    with pytest.raises(ValueError):store.save_statement_import(a,'Again',digest('first'),[row()])
    with pytest.raises(ValueError):store.save_statement_import(a,'Overlap',digest('second'),[row()])
    with pytest.raises(ValueError):store.save_statement_import(a,'Invalid',digest('bad'),[dict(row(),action='Add to ledger'),dict(row(),kind='Review required')])
    assert store.list_transactions(a)==[]
    assert len(store.list_statements(a))==1
    with pytest.raises(ValueError):store.delete_statement(b,result['id'])
    store.delete_statement(a,result['id']);assert not store.list_statement_entries(a)


def test_ledger_add_and_owned_matching_links(store,accounts):
    a,b,_=[u['id'] for u in accounts]
    saved=store.save_statement_import(a,'Linked',digest('link'),[dict(row(),action='Add to ledger')])
    tx=store.list_transactions(a)[0];entry=store.list_statement_entries(a)[0]
    assert entry['ledger_transaction_id']==tx['id']
    assert len(store.list_transactions(a))==1
    with pytest.raises(ValueError):store.connect_statement_entry(a,entry['id'],'Add to ledger')
    with pytest.raises(ValueError):store.save_statement_import(b,'Foreign',digest('foreign'),[dict(row(),action='Link existing',target_id=tx['id'])])
    store.delete_statement(a,saved['id']);assert store.list_transactions(a)[0]['id']==tx['id']
    store.save_statement_import(a,'Link again',digest('again'),[dict(row(),action='Link existing',target_id=tx['id'])])
    assert len(store.list_transactions(a))==1


def test_shared_link_preserves_single_expense(store,accounts,shared_group):
    a,b,_=[u['id'] for u in accounts]
    expense=store.create_shared_expense(a,shared_group['id'],'Swiggy',25000,'Food & drinks','2026-11-15',a,{a:12500,b:12500})
    store.save_statement_import(a,'Shared',digest('shared'),[dict(row(),action='Link shared expense',target_id=expense['id'])])
    assert store.list_statement_entries(a)[0]['shared_expense_id']==expense['id']
    assert store.list_transactions(a)==[]
    with pytest.raises(ValueError):store.save_statement_import(b,'Not payer',digest('notpayer'),[dict(row(),action='Link shared expense',target_id=expense['id'])])


def test_agent_statement_scope_and_redaction(store,accounts,monkeypatch):
    a,b,_=[u['id'] for u in accounts]
    store.save_statement_import(a,'Private',digest('ai'),[dict(row(),description='UPI/123456789012 name@ybl Swiggy')])
    agent=Agent(store,a)
    assert agent._run_read_tool('statement_summary',{'day':'2026-11-15'})['spending_cents']==25000
    result=agent._run_read_tool('statement_transactions',{'day':'2026-11-15'})
    assert '123456789012' not in str(result) and 'name@ybl' not in str(result)
    assert Agent(store,b)._run_read_tool('statement_transactions',{'day':'2026-11-15'})['records']==[]
    answer=agent._local_respond('bank statement 2026-11-15')
    assert '250' in answer['message'] and 'Swiggy' in answer['message']


def test_scanned_statement_ocr():
    import shutil
    if not shutil.which('tesseract'):pytest.skip('Tesseract installed in production Docker image')
    from PIL import Image,ImageDraw,ImageFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader
    font='/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
    from pathlib import Path
    if not Path(font).exists():pytest.skip('Synthetic OCR test requires DejaVu font')
    im=Image.new('RGB',(1600,500),'white');d=ImageDraw.Draw(im);f=ImageFont.truetype(font,28)
    for i,line in enumerate(['Date          Description           Amount','15/11/2026    PhonePe Swiggy         250.00 DR','16/11/2026    Google Pay refund      100.00 CR']):d.text((30,40+i*80),line,font=f,fill='black')
    b=io.BytesIO();c=Canvas(b,pagesize=(800,250));c.drawImage(ImageReader(im),0,0,width=800,height=250);c.save()
    result=parse_pdf(b.getvalue())
    assert result['ocr']
    assert [(r['amount_cents'],r['kind']) for r in result['rows']]==[(25000,'expense'),(10000,'income')]


def test_transfer_cannot_inflate_ledger_and_matching_date_required(store,accounts):
    uid=accounts[0]['id']
    with pytest.raises(ValueError,match='Transfers'):store.save_statement_import(uid,'Transfer',digest('transfer'),[dict(row(),classification='transfer',action='Add to ledger')])
    target=store.create_transaction(uid,'Lunch',25000,'Food & drinks','2026-11-14')
    with pytest.raises(ValueError,match='match'):store.save_statement_import(uid,'Mismatch',digest('mismatch'),[dict(row(),action='Link existing',target_id=target['id'])])
    assert store.list_statements(uid)==[]
