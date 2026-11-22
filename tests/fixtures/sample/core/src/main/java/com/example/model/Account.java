package com.example.model;

public class Account extends Entity {
    private final String number;

    public Account(String number) {
        this.number = number;
        setState(1);
    }

    @Override
    public String uid() {
        return "A-" + number;
    }

    /** Fluent builder for accounts. */
    public static class Builder {
        private String number;

        public Builder number(String number) {
            this.number = number;
            return this;
        }

        public Account build() {
            return new Account(number);
        }
    }
}
