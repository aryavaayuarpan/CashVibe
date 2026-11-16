"""Private statement upload, review, reconciliation and cash-flow analytics."""
from __future__ import annotations
import csv
import hashlib
import io
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from cashvibe.statement_parser import APPS, PLATFORMS, MAX_BYTES, CATEGORIES, cents, csv_rows, parse_pdf
from cashvibe.ui import CURRENCY, money, category_chart, chart_style


def export_statement_csv(rows):
    columns = ['occurred_on','description','amount','kind','classification','category','payment_app','platform','reference','ledger_transaction_id','shared_expense_id']
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=columns);writer.writeheader()
    for row in rows:
        record={key:row.get(key,'') for key in columns}
        record['amount']=f"{row['amount_cents']/100:.2f}"
        for key,value in record.items():
            if isinstance(value,str) and (value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n'))):
                record[key]="'"+value
        writer.writerow(record)
    return out.getvalue().encode('utf-8-sig')


def _frame(rows):
    return pd.DataFrame([{
        'Date':date.fromisoformat(r['occurred_on']), 'Description':r['description'],
        'Amount':r['amount_cents']/100, 'Direction':r['kind'],
        'Classification':r.get('classification',r['kind'] if r['kind'] in ('expense','income') else 'expense'),
        'Category':r['category'], 'Payment app':r['payment_app'], 'Merchant platform':r['platform'],
        'Action':'Statement only', 'Keep distinct':False,
        'Review note':r.get('review_note',''), 'Reference':r.get('reference',''), 'Balance minor units':r.get('balance_cents'),
    } for r in rows])


def _review(store,uid,key):
    draft=st.session_state.get(key)
    if not draft:return
    st.subheader('Review extracted transactions')
    st.caption('expense = money out; income = money in. Mark internal transfers as transfer. Suggested categories and apps are editable. Check every amount against your bank statement.')
    for warning in draft['warnings'][:15]:st.warning(warning)
    if draft['warnings']:
        st.info('Some rows may be missing or uncertain. Reconcile the preview with the statement; correct the source CSV or add rows below before saving.')
    label=st.text_input('Statement name',value='Bank statement',max_chars=100,key=key+'label')
    edited=st.data_editor(_frame(draft['rows']),key=key+'editor',num_rows='dynamic',hide_index=True,width='stretch',
        column_config={
            'Date':st.column_config.DateColumn(required=True,format='YYYY-MM-DD'),
            'Amount':st.column_config.NumberColumn(f'Amount ({CURRENCY})',min_value=.01,format='%.2f',required=True),
            'Direction':st.column_config.SelectboxColumn(options=['expense','income','Review required'],required=True),
            'Classification':st.column_config.SelectboxColumn(options=['expense','income','transfer'],required=True),
            'Category':st.column_config.SelectboxColumn(options=CATEGORIES,required=True),
            'Payment app':st.column_config.SelectboxColumn(options=APPS,required=True),
            'Merchant platform':st.column_config.SelectboxColumn(options=PLATFORMS,required=True),
            'Action':st.column_config.SelectboxColumn(options=['Statement only','Add to ledger','Skip'],required=True),
            'Keep distinct':st.column_config.CheckboxColumn(help='Save this as a separate transaction even if it matches an earlier statement import.'),
        'Reference':None, 'Balance minor units':None,
        },disabled=['Review note','Reference','Balance minor units'])
    rows=[]
    try:
        for index,item in enumerate(edited.to_dict('records')):
            if item.get('Action')=='Skip':rows.append({'action':'Skip'});continue
            day=item['Date']; day=day.isoformat() if hasattr(day,'isoformat') else str(day)
            rows.append(dict(occurred_on=day[:10],description=item['Description'],amount_cents=cents(item['Amount']),
                kind=item['Direction'],classification=item['Classification'],category=item['Category'],
                payment_app=item['Payment app'],platform=item['Merchant platform'],action=item['Action'],
                keep_distinct=bool(item.get('Keep distinct',False)),reference=str(item.get('Reference') or '') if pd.notna(item.get('Reference')) else '',balance_cents=int(item['Balance minor units']) if pd.notna(item.get('Balance minor units')) else None))
        downloadable=[r for r in rows if r.get('action')!='Skip']
        st.download_button('Download converted CSV',export_statement_csv(downloadable),'cashvibe-converted.csv','text/csv',key=key+'csv')
    except (ValueError,TypeError,KeyError):
        st.info('Complete the review table to enable CSV conversion and saving.');rows=[]
    st.caption('Statement only keeps these cash flows separate from your ledger and budgets. Add to ledger creates a personal transaction. You can link existing personal or shared records after saving. Matching rows in earlier imports are skipped unless Keep distinct is checked.')
    approved=st.checkbox('I checked the dates, amounts, directions and completeness against my statement.',key=key+'approve')
    a,b=st.columns(2)
    if a.button('Save reviewed statement',type='primary',disabled=not rows or not approved,key=key+'save'):
        try:
            result=store.save_statement_import(uid,label,draft['hash'],rows)
            st.session_state.pop(key,None);st.session_state[key+'upload_version']=st.session_state.get(key+'upload_version',0)+1
            st.session_state[key+'notice']=f"Saved {result['saved']} transactions; skipped {result['skipped']} duplicates or excluded rows."
            st.rerun()
        except (ValueError,PermissionError) as exc:st.error(str(exc))
    if b.button('Discard preview',key=key+'discard'):
        st.session_state.pop(key,None);st.session_state[key+'upload_version']=st.session_state.get(key+'upload_version',0)+1;st.rerun()


def _upload(store,uid,key):
    st.caption('PDF or CSV · up to 5 MB · 2,000 transactions · 50 PDF pages, including up to 5 scanned pages. Passwords are used only to open the PDF; original files are not saved in the database.')
    version=st.session_state.get(key+'upload_version',0)
    upload=st.file_uploader('Upload bank statement',type=['pdf','csv'],key=key+f'upload{version}')
    if upload:
        if upload.size>MAX_BYTES:st.error('Maximum file size is 5 MB.');return
        is_pdf=upload.name.lower().endswith('.pdf')
        password=st.text_input('PDF password (only if encrypted)',type='password',key=key+f'password{version}') if is_pdf else ''
        mapping=None;day_first=True
        if not is_pdf:
            with st.expander('CSV column mapping (optional)'):
                manual=st.checkbox('Choose columns manually',key=key+'manual')
                day_first=st.checkbox('Day comes before month in dates',value=True,key=key+'dayfirst')
                if manual:
                    try:
                        text=upload.getvalue().decode('utf-8-sig')
                        try:dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
                        except csv.Error:dialect=csv.excel
                        headers=csv.DictReader(io.StringIO(text),dialect=dialect).fieldnames or []
                        mapping={field:st.selectbox(field.replace('_',' ').title(),['']+headers,key=key+'map'+field) for field in ['date','description','debit','credit','amount','direction','balance']}
                    except (UnicodeError,csv.Error):st.error('Please use UTF-8 CSV.')
        if st.button('Convert and review',key=key+'convert'):
            try:
                with st.spinner('Reading statement locally…'):
                    data=upload.getvalue()
                    if is_pdf:parsed=parse_pdf(data,password)
                    else:
                        rows,warnings,_=csv_rows(data,mapping,day_first);parsed={'rows':rows,'warnings':warnings}
                    if not parsed['rows']:raise ValueError('No transactions found. Try CSV column mapping.')
                    st.session_state[key]={**parsed,'hash':hashlib.sha256(data).hexdigest()}
                    st.session_state[key+'upload_version']=version+1
                st.rerun()
            except ValueError as exc:st.error(str(exc))
    _review(store,uid,key)


def _connect(store,uid,rows,key):
    candidates=[r for r in rows if not r['ledger_transaction_id'] and not r['shared_expense_id']]
    if not candidates:return
    with st.expander('Connect a statement transaction to your ledger or group'):
        row=st.selectbox('Statement transaction',candidates,format_func=lambda r:f"{r['occurred_on']} · {r['description']} · {money(r['amount_cents'])}",key=key+'connectrow')
        action=st.radio('Connection',['Link existing','Link shared expense','Add to ledger'],key=key+'connectaction',horizontal=True)
        target=None
        if action!='Add to ledger':
            target_type='personal' if action=='Link existing' else 'shared'
            targets=[t for t in store.statement_link_targets(uid) if t['target_type']==target_type and (t['occurred_on'],t['amount_cents'],t['kind'])==(row['occurred_on'],row['amount_cents'],row['kind'])]
            if targets:target=st.selectbox('Matching record',targets,format_func=lambda t:t['description']+' · '+t.get('group_name','Personal'),key=key+'target')
            else:st.info('No matching record. Linking requires the same date, amount and direction; shared expenses must have been paid by you.')
        st.caption('Linking keeps the existing ledger record; it does not create another expense. Adding to ledger affects budgets. Transfers cannot be added as spending.')
        if st.button('Confirm connection',disabled=action!='Add to ledger' and not target,key=key+'connect'):
            try:store.connect_statement_entry(uid,row['id'],action,target['id'] if target else None);st.rerun()
            except (ValueError,PermissionError) as exc:st.error(str(exc))


def render_personal_spendings(store,uid,month):
    key='statement_'+uid
    st.title('Personal spendings')
    st.write('Turn bank statements into a clear picture of your money: review, categorize, explore and ask SpendGenie.')
    notice=st.session_state.pop(key+'notice',None)
    if notice:st.success(notice)
    upload_tab,analysis_tab=st.tabs(['Upload & convert','Spending analysis'],key=key+'tabs',on_change='rerun')
    with upload_tab:_upload(store,uid,key)
    with analysis_tab:
        statements=store.list_statements(uid)
        if not statements:st.info('Upload and save a reviewed statement to see your spending analysis.');return
        options={'All statements':None,**{f"{s['label']} · {s['created_at'][:10]} · {s['id'][:6]}":s['id'] for s in statements}}
        selection=st.selectbox('Saved statement',list(options),key=key+'selection');sid=options[selection]
        months=sorted({r['occurred_on'][:7] for r in store.list_statement_entries(uid,statement_id=sid)},reverse=True)
        period=st.selectbox('Analysis month',months,index=months.index(month) if month in months else 0,key=key+'month')
        rows=store.list_statement_entries(uid,month=period,statement_id=sid)
        days=['Entire month']+sorted({r['occurred_on'] for r in rows},reverse=True)
        day=st.selectbox('Specific day',days,key=key+'day')
        if day!='Entire month':rows=[r for r in rows if r['occurred_on']==day]
        outgoing=sum(r['amount_cents'] for r in rows if r['kind']=='expense');incoming=sum(r['amount_cents'] for r in rows if r['kind']=='income')
        spending=[r for r in rows if r['classification']=='expense'];spent=sum(r['amount_cents'] for r in spending)
        for col,label,value in zip(st.columns(4),['Money out','Money received','Spending (excluding transfers)','Net cash flow'],[outgoing,incoming,spent,incoming-outgoing]):col.metric(label,money(value))
        st.caption('Bank cash flows include transfers. Spending excludes rows marked transfer. Statement totals can overlap linked ledger records; do not add them together. Currency: '+CURRENCY+'. Import statements in this currency only.')
        if not rows:st.info('No records in this selection.');return
        df=pd.DataFrame([{'Date':r['occurred_on'],'Money out':r['amount_cents']/100 if r['kind']=='expense' else 0,'Money in':r['amount_cents']/100 if r['kind']=='income' else 0,'Payment app':r['payment_app'],'Merchant platform':r['platform']} for r in rows])
        daily=df.groupby('Date')[['Money out','Money in']].sum().reset_index()
        st.plotly_chart(chart_style(px.bar(daily,x='Date',y=['Money out','Money in'],barmode='group',title='Daily cash flow')),width='stretch')
        apps=df.groupby('Payment app')[['Money out','Money in']].sum().reset_index()
        st.plotly_chart(chart_style(px.bar(apps,x='Payment app',y=['Money out','Money in'],barmode='group',title='Money by payment app')),width='stretch')
        categories={};platforms={}
        for row in spending:
            categories[row['category']]=categories.get(row['category'],0)+row['amount_cents']
            platforms[row['platform']]=platforms.get(row['platform'],0)+row['amount_cents']
        left,right=st.columns(2)
        with left:category_chart(categories)
        with right:
            if platforms:st.plotly_chart(chart_style(px.bar(x=list(platforms),y=[v/100 for v in platforms.values()],labels={'x':'Merchant platform','y':CURRENCY},title='Spending by merchant platform')),width='stretch')
        st.subheader('Your spending patterns')
        if spending:
            top=max(categories,key=categories.get);peak=max(spending,key=lambda r:r['amount_cents'])
            st.write(f"Your largest category is {top}: {money(categories[top])} ({categories[top]/spent:.0%} of spending). Your largest purchase is {money(peak['amount_cents'])} on {peak['occurred_on']}.")
            st.write(f"Average spending per active spending day: {money(round(spent/len({r['occurred_on'] for r in spending})))}. Based on {len(spending)} reviewed spending transactions.")
        st.caption('App names are identified only when the statement explicitly names them or you confirm them. UPI handles alone do not establish which app was used. SpendGenie can read these saved records when you ask about bank statements, apps or merchant platforms.')
        st.dataframe(pd.DataFrame([{'Date':r['occurred_on'],'Description':r['description'],'Amount':r['amount_cents']/100,'Direction':r['kind'],'Classification':r['classification'],'Category':r['category'],'Payment app':r['payment_app'],'Merchant platform':r['platform'],'Linked':bool(r['ledger_transaction_id'] or r['shared_expense_id'])} for r in rows]),hide_index=True,width='stretch')
        st.download_button('Download saved CSV',export_statement_csv(rows),'cashvibe-personal-spendings.csv','text/csv',key=key+'savedcsv')
        _connect(store,uid,rows,key)
        if sid:
            with st.expander('Delete this statement'):
                confirm=st.checkbox('Delete statement analysis; keep any linked ledger or group records.',key=key+'deleteconfirm')
                if st.button('Delete statement',disabled=not confirm,key=key+'delete'):
                    store.delete_statement(uid,sid);st.rerun()
