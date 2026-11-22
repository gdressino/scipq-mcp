package com.example.model;

public class SubAccount extends Account {
    public SubAccount(String number) {
        super(number);
    }

    @Override
    public String uid() {
        return "S-" + super.uid();
    }
}
