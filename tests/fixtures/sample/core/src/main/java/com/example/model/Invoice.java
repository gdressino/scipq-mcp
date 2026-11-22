package com.example.model;

/**
 * scip-javac reports this type at the first textual occurrence of its name after the start of
 * its range: the string in @Label below, not the declaration.
 */
@Label("Invoice")
@Stored(cached = true,
        table = "INVOICES")
public class Invoice {
    private long number;

    public long getNumber() {
        return number;
    }
}
