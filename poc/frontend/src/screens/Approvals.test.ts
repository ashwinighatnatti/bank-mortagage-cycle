/**
 * The authorisation queue has one job: let a supervisor tell two requests apart
 * before committing money to either. It failed that once — four cards headed
 * "order_vendor_service" with the loan id beside them, differing only in a word
 * buried in a grey argument string, and they read as duplicates.
 */

import { describe as suite, expect, it } from 'vitest'
import { describe, groupByLoan } from './Approvals'
import type { ConfirmationRow } from '../api'

const row = (loan: string, args: Record<string, unknown>, tool = 'order_vendor_service') =>
  ({
    token: `${tool}|${JSON.stringify(args)}`,
    loan_id: loan,
    tool,
    args,
    requested_by: 'processing',
    requested_at: '2026-08-20T09:00:00Z',
    status: 'pending',
    confirmed_by: null,
  }) as ConfirmationRow

suite('describing a gated action', () => {
  it('distinguishes two services on the same loan', () => {
    const appraisal = describe('order_vendor_service', { service: 'appraisal' })
    const title = describe('order_vendor_service', { service: 'title' })

    expect(appraisal).not.toBe(title)
    expect(appraisal).toMatch(/appraisal/i)
    expect(title).toMatch(/title/i)
  })

  it('never falls back to the bare function name for a known tool', () => {
    for (const service of ['appraisal', 'title', 'field_review', 'flood_determination',
                           'credit_repull', 'avm']) {
      expect(describe('order_vendor_service', { service })).not.toContain('order_vendor_service')
    }
  })

  it('still says something useful for a service it has no label for', () => {
    expect(describe('order_vendor_service', { service: 'survey' })).toBe('Order survey')
  })

  it('names the document when a borrower is being contacted', () => {
    expect(describe('request_borrower_document', { document_kind: 'bank_statement' }))
      .toBe('Contact the borrower for a bank statement')
  })
})

suite('grouping', () => {
  it('puts several actions on one file under one heading', () => {
    const grouped = groupByLoan([
      row('LN-2026-0007', { service: 'title' }),
      row('LN-2026-0002', { service: 'appraisal' }),
      row('LN-2026-0002', { service: 'title' }),
    ])

    expect(grouped.map(([loan]) => loan)).toEqual(['LN-2026-0002', 'LN-2026-0007'])
    expect(grouped[0][1]).toHaveLength(2)
  })

  it('gives every pending row a distinct label within its loan', () => {
    const rows = [
      row('LN-2026-0002', { loan_id: 'LN-2026-0002', service: 'appraisal', reason: 'collateral review' }),
      row('LN-2026-0002', { loan_id: 'LN-2026-0002', service: 'title', reason: 'collateral review' }),
    ]
    const labels = rows.map((r) => describe(r.tool, r.args))
    expect(new Set(labels).size).toBe(rows.length)
  })
})
