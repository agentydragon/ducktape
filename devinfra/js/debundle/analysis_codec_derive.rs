//! Typed artifact records, independent of a type's report Serde attributes.

use proc_macro::TokenStream;
use proc_macro2::TokenStream as Tokens;
use quote::{format_ident, quote};
use syn::{Data, DeriveInput, Fields, Index, parse_macro_input, parse_quote};

#[proc_macro_derive(Artifact)]
pub fn derive_artifact(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);
    expand(input)
        .unwrap_or_else(syn::Error::into_compile_error)
        .into()
}

fn expand(input: DeriveInput) -> syn::Result<Tokens> {
    let name = &input.ident;
    let mut generics = input.generics.clone();
    for parameter in generics.type_params_mut() {
        parameter
            .bounds
            .push(parse_quote!(::analysis_codec::Artifact));
    }
    let (implementation, arguments, constraints) = generics.split_for_impl();
    let (length, write, read) = match &input.data {
        Data::Struct(data) => {
            let length = data.fields.len();
            let writes = data.fields.iter().enumerate().map(|(index, field)| {
                let member = field.ident.as_ref().map_or_else(
                    || syn::Member::Unnamed(Index::from(index)),
                    |name| syn::Member::Named(name.clone()),
                );
                quote! {
                    ::serde::ser::SerializeTuple::serialize_element(
                        tuple, &::analysis_codec::Encoded::new(&self.#member, context),
                    )?;
                }
            });
            let reads = read_fields(&data.fields, 0);
            let construct = construct(quote!(Self), &data.fields);
            (
                quote!(#length),
                quote!(#(#writes)*),
                quote!(#reads Ok(#construct)),
            )
        }
        Data::Enum(data) => {
            let lengths = data.variants.iter().map(|variant| {
                let variant_name = &variant.ident;
                let pattern = ignore_pattern(&variant.fields);
                let length = variant.fields.len() + 1;
                quote!(Self::#variant_name #pattern => #length)
            });
            let writes = data.variants.iter().enumerate().map(|(ordinal, variant)| {
                let variant_name = &variant.ident;
                let ordinal = ordinal as u32;
                let pattern = construct(quote!(Self::#variant_name), &variant.fields);
                let fields = variant.fields.iter().enumerate().map(|(index, _)| {
                    let name = format_ident!("__field_{index}");
                    quote! {
                        ::serde::ser::SerializeTuple::serialize_element(
                            tuple, &::analysis_codec::Encoded::new(#name, context),
                        )?;
                    }
                });
                quote! {
                    #pattern => {
                        ::serde::ser::SerializeTuple::serialize_element(tuple, &#ordinal)?;
                        #(#fields)*
                    }
                }
            });
            let reads = data.variants.iter().enumerate().map(|(ordinal, variant)| {
                let ordinal = ordinal as u32;
                let variant_name = &variant.ident;
                let fields = read_fields(&variant.fields, 1);
                let construct = construct(quote!(Self::#variant_name), &variant.fields);
                quote!(#ordinal => { #fields Ok(#construct) })
            });
            (
                quote!(match self { #(#lengths),* }),
                quote!(match self { #(#writes),* }),
                quote! {
                    let tag: u32 = ::analysis_codec::read_field(sequence, context, 0)?;
                    match tag {
                        #(#reads),*,
                        _ => Err(::serde::de::Error::custom(concat!("unknown artifact variant for ", stringify!(#name)))),
                    }
                },
            )
        }
        Data::Union(_) => {
            return Err(syn::Error::new_spanned(
                name,
                "artifact unions are unsupported",
            ));
        }
    };
    Ok(quote! {
        impl #implementation ::analysis_codec::ArtifactRecord for #name #arguments #constraints {
            fn tuple_len(&self) -> usize { #length }

            fn write_fields<__S: ::serde::ser::SerializeTuple>(
                &self,
                tuple: &mut __S,
                context: &::analysis_codec::ArtifactContext<'_>,
            ) -> Result<(), __S::Error> {
                #write
                Ok(())
            }

            fn read_fields<'__de, __A: ::serde::de::SeqAccess<'__de>>(
                sequence: &mut __A,
                context: &::analysis_codec::ArtifactContext<'_>,
            ) -> Result<Self, __A::Error> {
                #read
            }
        }

        impl #implementation ::analysis_codec::Artifact for #name #arguments #constraints {
            fn encode<__S: ::serde::Serializer>(
                &self,
                serializer: __S,
                context: &::analysis_codec::ArtifactContext<'_>,
            ) -> Result<__S::Ok, __S::Error> {
                ::analysis_codec::encode_record(self, serializer, context)
            }

            fn decode<'__de, __D: ::serde::Deserializer<'__de>>(
                deserializer: __D,
                context: &::analysis_codec::ArtifactContext<'_>,
            ) -> Result<Self, __D::Error> {
                ::analysis_codec::decode_record(deserializer, context)
            }
        }
    })
}

fn read_fields(fields: &Fields, offset: usize) -> Tokens {
    let fields = fields.iter().enumerate().map(|(index, field)| {
        let name = format_ident!("__field_{index}");
        let ty = &field.ty;
        let position = index + offset;
        quote!(let #name: #ty = ::analysis_codec::read_field(sequence, context, #position)?;)
    });
    quote!(#(#fields)*)
}

fn construct(prefix: Tokens, fields: &Fields) -> Tokens {
    let bindings = fields.iter().enumerate().map(|(index, field)| {
        let binding = format_ident!("__field_{index}");
        match &field.ident {
            Some(name) => quote!(#name: #binding),
            None => quote!(#binding),
        }
    });
    match fields {
        Fields::Named(_) => quote!(#prefix { #(#bindings),* }),
        Fields::Unnamed(_) => quote!(#prefix ( #(#bindings),* )),
        Fields::Unit => prefix,
    }
}

fn ignore_pattern(fields: &Fields) -> Tokens {
    match fields {
        Fields::Named(_) => quote!({ .. }),
        Fields::Unnamed(_) => quote!((..)),
        Fields::Unit => quote!(),
    }
}
