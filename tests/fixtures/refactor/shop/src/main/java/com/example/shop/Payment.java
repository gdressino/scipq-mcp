package com.example.shop;

import javax.jdo.annotations.PersistenceCapable;

/** Points to its payer through an interface. */
@PersistenceCapable(table = "PAYMENTS")
public class Payment {
    private Buyer payer;

    private String code;

    public Buyer getPayer() {
        return payer;
    }
}
