import javax.xml.crypto.*;
import javax.xml.crypto.dsig.*;
import javax.xml.crypto.dsig.dom.*;
import javax.xml.crypto.dsig.keyinfo.*;
import javax.xml.crypto.dsig.spec.*;
import javax.xml.parsers.*;
import javax.xml.transform.*;
import javax.xml.transform.dom.*;
import javax.xml.transform.stream.*;
import org.w3c.dom.*;
import org.xml.sax.InputSource;
import java.io.*;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.Base64;

/**
 * FirmaDTE.java — Firma XMLDSig para SII Chile
 *
 * Modos:
 *   firmar-sobre-completo <sobre_b64> <pfx_b64> <password>
 *     → Firma todos los DTEs del sobre y luego el SetDTE
 *     → Devuelve el sobre completo firmado en Base64
 *
 *   firmar-dte  <xml_b64> <pfx_b64> <password> <doc_id>  (legacy)
 *   firmar-sobre <xml_b64> <pfx_b64> <password>           (legacy)
 */
public class FirmaDTE {

    static final String NS_SII    = "http://www.sii.cl/SiiDte";
    static final String NS_XMLDSIG = "http://www.w3.org/2000/09/xmldsig#";
    static final String C14N      = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315";

    public static void main(String[] args) throws Exception {
        if (args.length < 4) {
            System.err.println("Uso: java FirmaDTE <modo> <xml_b64> <pfx_b64> <password> [doc_id]");
            System.exit(1);
        }

        String modo     = args[0];
        byte[] xmlBytes = Base64.getDecoder().decode(args[1]);
        byte[] pfxBytes = Base64.getDecoder().decode(args[2]);
        String password = args[3];

        KeyStore ks = KeyStore.getInstance("PKCS12");
        ks.load(new ByteArrayInputStream(pfxBytes), password.toCharArray());
        String alias      = ks.aliases().nextElement();
        PrivateKey privKey = (PrivateKey) ks.getKey(alias, password.toCharArray());
        X509Certificate cert = (X509Certificate) ks.getCertificate(alias);

        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        DocumentBuilder builder = dbf.newDocumentBuilder();
        InputSource is = new InputSource(
            new InputStreamReader(new ByteArrayInputStream(xmlBytes), "ISO-8859-1")
        );
        Document doc = builder.parse(is);

        if (modo.equals("firmar-sobre-completo")) {
            firmarSobreCompleto(doc, privKey, cert);
        } else if (modo.equals("firmar-dte")) {
            String docId = args.length > 4 ? args[4] : "";
            firmarDTE(doc, privKey, cert, docId);
        } else if (modo.equals("firmar-sobre")) {
            firmarSobre(doc, privKey, cert);
        } else if (modo.equals("firmar-libro")) {
            firmarLibro(doc, privKey, cert);
        } else {
            System.err.println("Modo desconocido: " + modo);
            System.exit(1);
        }

        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        Transformer t = TransformerFactory.newInstance().newTransformer();
        t.setOutputProperty(OutputKeys.ENCODING, "ISO-8859-1");
        t.setOutputProperty(OutputKeys.OMIT_XML_DECLARATION, "yes");
        t.transform(new DOMSource(doc), new StreamResult(baos));

        byte[] xmlDecl = "<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?>\n"
                         .getBytes("ISO-8859-1");
        byte[] xmlBody = baos.toByteArray();
        byte[] result  = new byte[xmlDecl.length + xmlBody.length];
        System.arraycopy(xmlDecl, 0, result, 0, xmlDecl.length);
        System.arraycopy(xmlBody, 0, result, xmlDecl.length, xmlBody.length);

        System.out.print(Base64.getEncoder().encodeToString(result));
    }

    // ── NUEVO MODO: firma todos los DTEs en el sobre + el SetDTE ──────────────
    static void firmarSobreCompleto(Document doc, PrivateKey privKey,
                                     X509Certificate cert) throws Exception {
        // 1. Firmar cada DTE dentro del sobre
        NodeList dteList = doc.getElementsByTagNameNS(NS_SII, "DTE");
        for (int i = 0; i < dteList.getLength(); i++) {
            Element dteEl  = (Element) dteList.item(i);
            Element docEl  = (Element) dteEl.getElementsByTagNameNS(NS_SII, "Documento").item(0);
            String  docId  = docEl.getAttribute("ID");
            docEl.setIdAttribute("ID", true);
            _firmarElemento(doc, dteEl, docId, privKey, cert);
        }

        // 2. Firmar SetDTE
        Element setDTE = (Element) doc.getElementsByTagNameNS(NS_SII, "SetDTE").item(0);
        setDTE.setIdAttribute("ID", true);
        Element envioEl = doc.getDocumentElement();
        _firmarElemento(doc, envioEl, "SetDoc", privKey, cert);
    }

    static void _firmarElemento(Document doc, Element parent, String refId,
                                  PrivateKey privKey, X509Certificate cert)
            throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(C14N, (TransformParameterSpec) null)
        );

        Reference ref = fac.newReference(
            "#" + refId,
            fac.newDigestMethod(DigestMethod.SHA1, null),
            transforms, null, null
        );

        SignedInfo si = fac.newSignedInfo(
            fac.newCanonicalizationMethod(C14N, (C14NMethodParameterSpec) null),
            fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
            Collections.singletonList(ref)
        );

        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue  kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data  x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo   ki   = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);
        DOMSignContext dsc = new DOMSignContext(privKey, parent);
        signature.sign(dsc);
    }

    // ── MODOS LEGACY ──────────────────────────────────────────────────────────
    static void firmarDTE(Document doc, PrivateKey privKey,
                           X509Certificate cert, String docId) throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(C14N, (TransformParameterSpec) null)
        );
        Reference ref = fac.newReference(
            "#" + docId,
            fac.newDigestMethod(DigestMethod.SHA1, null),
            transforms, null, null
        );
        SignedInfo si = fac.newSignedInfo(
            fac.newCanonicalizationMethod(C14N, (C14NMethodParameterSpec) null),
            fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
            Collections.singletonList(ref)
        );
        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue  kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data  x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo   ki   = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);

        NodeList docNodes = doc.getElementsByTagNameNS(NS_SII, "Documento");
        if (docNodes.getLength() == 0) docNodes = doc.getElementsByTagName("Documento");
        ((Element) docNodes.item(0)).setIdAttribute("ID", true);

        NodeList dteNodes = doc.getElementsByTagNameNS(NS_SII, "DTE");
        if (dteNodes.getLength() == 0) dteNodes = doc.getElementsByTagName("DTE");
        Element dteEl = (Element) dteNodes.item(0);

        DOMSignContext dsc = new DOMSignContext(privKey, dteEl);
        signature.sign(dsc);
    }

    static void firmarSobre(Document doc, PrivateKey privKey,
                             X509Certificate cert) throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(C14N, (TransformParameterSpec) null)
        );
        Reference ref = fac.newReference(
            "#SetDoc",
            fac.newDigestMethod(DigestMethod.SHA1, null),
            transforms, null, null
        );
        SignedInfo si = fac.newSignedInfo(
            fac.newCanonicalizationMethod(C14N, (C14NMethodParameterSpec) null),
            fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
            Collections.singletonList(ref)
        );
        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue  kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data  x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo   ki   = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);

        NodeList setNodes = doc.getElementsByTagNameNS(NS_SII, "SetDTE");
        if (setNodes.getLength() == 0) setNodes = doc.getElementsByTagName("SetDTE");
        ((Element) setNodes.item(0)).setIdAttribute("ID", true);

        Element envioEl = doc.getDocumentElement();
        DOMSignContext dsc = new DOMSignContext(privKey, envioEl);
        signature.sign(dsc);
    }

    // ── MODO: firmar-libro ─────────────────────────────────────────────────────
    // Firma el elemento EnvioLibro con ID="LibroVentas" dentro de LibroCompraVenta
    static void firmarLibro(Document doc, PrivateKey privKey, X509Certificate cert)
            throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        // Obtener el ID real del EnvioLibro (puede ser LibroVentas o LibroCompras)
        NodeList libroNodesRef = doc.getElementsByTagNameNS(NS_SII, "EnvioLibro");
        if (libroNodesRef.getLength() == 0) libroNodesRef = doc.getElementsByTagName("EnvioLibro");
        String libroId = ((Element) libroNodesRef.item(0)).getAttribute("ID");
        if (libroId == null || libroId.isEmpty()) libroId = "LibroVentas";

        Reference ref = fac.newReference(
                "#" + libroId,
                fac.newDigestMethod(DigestMethod.SHA1, null),
                Collections.singletonList(
                        fac.newTransform(Transform.ENVELOPED, (TransformParameterSpec) null)),
                null, null);

        SignedInfo si = fac.newSignedInfo(
                fac.newCanonicalizationMethod(CanonicalizationMethod.INCLUSIVE,
                        (C14NMethodParameterSpec) null),
                fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
                Collections.singletonList(ref));

        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo  ki   = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);

        // Registrar ID del EnvioLibro
        NodeList libroNodes = doc.getElementsByTagNameNS(NS_SII, "EnvioLibro");
        if (libroNodes.getLength() == 0) libroNodes = doc.getElementsByTagName("EnvioLibro");
        ((Element) libroNodes.item(0)).setIdAttribute("ID", true);

        Element rootEl = doc.getDocumentElement();
        DOMSignContext dsc = new DOMSignContext(privKey, rootEl);
        signature.sign(dsc);
    }
}
