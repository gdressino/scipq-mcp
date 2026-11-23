package com.example.shop;

/** Runs on a thread: the JDK calls run(). */
public class Ticker implements Runnable {
    Ticker() {
    }

    @Override
    public void run() {
        tick();
    }

    void tick() {
    }
}
