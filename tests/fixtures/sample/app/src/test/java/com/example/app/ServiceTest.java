package com.example.app;

import com.example.model.Account;
import com.example.model.Entity;
import com.example.model.SubAccount;

public class ServiceTest {
    public static void main(String[] args) {
        Entity e = Entity.findById(1L);
        Account a = new SubAccount("7");
        a.setState(8);
        new Service().activate(a);
        System.out.println(a.uid() + e);
    }
}
